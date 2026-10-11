from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit import assembly as asm
from circuit import cli, sexpr


def _fp(
    ref: str, lib_id: str, x: float, *, side: str = "F.Cu", attr: list[str] | None = None
) -> list[sexpr.SExpr]:
    items: list[sexpr.SExpr] = [
        "footprint",
        sexpr.quoted(lib_id),
        ["layer", sexpr.quoted(side)],
        ["at", str(x), "5", "90"],
        ["property", sexpr.quoted("Reference"), sexpr.quoted(ref)],
        ["property", sexpr.quoted("Value"), sexpr.quoted(f"{ref}-value")],
    ]
    if attr is not None:
        items.append(["attr", *attr])
    return items


def _pcb(path: Path, *extra: list[sexpr.SExpr], outline: bool = True) -> Path:
    root: list[sexpr.SExpr] = [
        "kicad_pcb",
        _fp("R1", "Resistor_SMD:R_0603", 5.0, attr=["smd"]),
        _fp("J1", "Connector_JST:B2B", 18.0, attr=["through_hole"]),
        _fp("C1", "Capacitor_SMD:C_0603", 8.0, side="B.Cu", attr=["smd"]),
        _fp("R9", "Resistor_SMD:R_0603", 9.0, attr=["smd", "dnp"]),
        _fp("H1", "MountingHole:MountingHole_3.2mm", 2.0, attr=["exclude_from_pos_files"]),
        _fp("G1", "Symbol:Logo", 12.0, attr=["board_only", "exclude_from_bom"]),
        *extra,
    ]
    if outline:
        root.insert(
            1,
            [
                "gr_poly",
                ["pts", ["xy", "0", "0"], ["xy", "20", "0"], ["xy", "20", "10"], ["xy", "0", "10"]],
                ["layer", sexpr.quoted("Edge.Cuts")],
            ],
        )
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    return path


def test_assembly_lists_populated_parts_by_mount_and_side(tmp_path: Path) -> None:
    result = asm.assembly(_pcb(tmp_path / "demo.kicad_pcb"))

    assert result.verdict == "pass", result.unknown
    parts = {p.ref: p for p in result.placements}
    assert set(parts) == {"C1", "J1", "R1", "R9"}
    assert (parts["R1"].mount, parts["R1"].side, parts["R1"].x_mm, parts["R1"].y_mm) == (
        "smd",
        "top",
        -5.0,
        0.0,
    )
    assert (parts["C1"].mount, parts["C1"].side) == ("smd", "bottom")
    assert parts["J1"].mount == "tht" and parts["J1"].value == "J1-value"
    assert parts["R9"].dnp and not parts["R1"].dnp
    summary = asm.assembly_result(result, tmp_path / "out.json")
    assert summary["dnp"] == 1
    assert summary["counts"] == {"smd_top": 1, "smd_bottom": 1, "tht_top": 1, "tht_bottom": 0}


def test_dnp_child_flag_is_honoured(tmp_path: Path) -> None:
    extra = _fp("R5", "Resistor_SMD:R_0603", 11.0, attr=["smd"])
    extra.append(["dnp", "yes"])
    result = asm.assembly(_pcb(tmp_path / "demo.kicad_pcb", extra))

    assert {p.ref: p.dnp for p in result.placements}["R5"] is True


@pytest.mark.parametrize(
    ("extra", "outline", "unknown"),
    [
        ([_fp("U1", "Package_SO:SOIC-8", 14.0)], True, ["mount:U1"]),
        ([_fp("R1", "Resistor_SMD:R_0603", 15.0, attr=["smd"])], True, ["duplicate:R1"]),
        ([], False, ["outline"]),
    ],
)
def test_incomplete_assembly_fails_closed(
    tmp_path: Path, extra: list[list[sexpr.SExpr]], outline: bool, unknown: list[str]
) -> None:
    result = asm.assembly(_pcb(tmp_path / "demo.kicad_pcb", *extra, outline=outline))

    assert result.verdict == "fail"
    assert result.unknown == unknown


def test_dnp_part_without_mount_attribute_is_not_unknown(tmp_path: Path) -> None:
    result = asm.assembly(
        _pcb(tmp_path / "demo.kicad_pcb", _fp("U2", "Package_SO:SOIC-8", 14.0, attr=["dnp"]))
    )

    assert result.verdict == "pass", result.unknown


def test_cli_writes_artifact_and_exit_code_follows_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "demo.circuit-assembly.json"
    assert (
        cli.main(["assembly", "--pcb", str(_pcb(tmp_path / "demo.kicad_pcb")), "--out", str(out)])
        == 0
    )
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["artifact_kind"] == "circuit_assembly"
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"

    bad = _pcb(tmp_path / "bad.kicad_pcb", _fp("U1", "Package_SO:SOIC-8", 14.0))
    assert cli.main(["assembly", "--pcb", str(bad), "--out", str(out)]) == 1
    assert (
        cli.main(["assembly", "--pcb", str(tmp_path / "missing.kicad_pcb"), "--out", str(out)]) == 1
    )
