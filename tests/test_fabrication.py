from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from circuit import cli, sexpr
from circuit.brief import DesignBrief
from circuit.fabrication import check_fabrication

BRIEF = Path(__file__).parent / "data" / "brief_desk_lamp.json"
SPEC: dict[str, Any] = {
    "copper_layers": 2,
    "thickness_mm": 1.6,
    "thickness_tolerance_mm": 0.16,
    "material": "FR-4 Tg150",
    "finish": "ENIG",
    "ipc6012_class": 2,
    "soldermask": "green",
    "silkscreen": "white",
}


def _brief(tmp_path: Path, **overrides: Any) -> Path:
    value: dict[str, Any] = json.loads(BRIEF.read_text(encoding="utf-8"))
    value["fabrication"] = {**SPEC, **overrides}
    path = tmp_path / "desk_lamp.brief.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _pcb(tmp_path: Path, copper: list[str] | None, thickness: str | None) -> Path:
    root: list[sexpr.SExpr] = ["kicad_pcb"]
    if thickness is not None:
        root.append(["general", ["thickness", thickness]])
    if copper is not None:
        layers: list[sexpr.SExpr] = ["layers"]
        for index, name in enumerate([*copper, "F.SilkS", "Edge.Cuts"]):
            kind = "signal" if name.endswith(".Cu") else "user"
            layers.append([str(index), sexpr.quoted(name), kind])
        root.append(layers)
    path = tmp_path / "desk_lamp.kicad_pcb"
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    return path


def _check(brief: Path, pcb: Path) -> Any:
    design = DesignBrief.model_validate_json(brief.read_text(encoding="utf-8"))
    return check_fabrication(design, brief, pcb)


def test_matching_board_passes_and_carries_the_spec(tmp_path: Path) -> None:
    report = _check(_brief(tmp_path), _pcb(tmp_path, ["F.Cu", "B.Cu"], "1.6"))
    assert report.verdict == "pass"
    assert report.spec.finish == "ENIG"
    assert [(c.aspect, c.measured) for c in report.checks] == [
        ("copper_layers", 2),
        ("thickness", 1.6),
    ]


@pytest.mark.parametrize(("thickness", "verdict"), [("1.76", "pass"), ("1.4399", "fail")])
def test_thickness_tolerance_boundary(tmp_path: Path, thickness: str, verdict: str) -> None:
    report = _check(_brief(tmp_path), _pcb(tmp_path, ["F.Cu", "B.Cu"], thickness))
    assert report.verdict == verdict


def test_layer_count_mismatch_fails(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "1.6")
    report = _check(_brief(tmp_path), pcb)
    assert report.verdict == "fail"
    assert report.checks[0].measured == 4


def test_silent_board_is_unknown(tmp_path: Path) -> None:
    report = _check(_brief(tmp_path), _pcb(tmp_path, None, None))
    assert report.verdict == "unknown"
    assert {c.status for c in report.checks} == {"unknown"}


@pytest.mark.parametrize("raw", [b"(kicad_pcb", b"(kicad_sch)", b"\xff"])
def test_unreadable_board_is_unknown(tmp_path: Path, raw: bytes) -> None:
    pcb = tmp_path / "bad.kicad_pcb"
    pcb.write_bytes(raw)
    report = _check(_brief(tmp_path), pcb)
    assert report.verdict == "unknown"
    assert "board unreadable" in report.checks[0].detail


def test_missing_board_is_unknown(tmp_path: Path) -> None:
    report = _check(_brief(tmp_path), tmp_path / "absent.kicad_pcb")
    assert (report.verdict, report.pcb_sha256) == ("unknown", None)


@pytest.mark.parametrize(
    "bad",
    [
        {"copper_layers": 0},
        {"thickness_mm": 0},
        {"thickness_tolerance_mm": 0},
        {"finish": "gold"},
        {"ipc6012_class": 4},
        {"material": ""},
        {"extra": 1},
    ],
)
def test_invalid_spec_is_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DesignBrief.model_validate_json(_brief(tmp_path, **bad).read_text(encoding="utf-8"))


def test_cli_exit_follows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = _brief(tmp_path)
    out = tmp_path / "desk_lamp.fabrication.json"
    good = _pcb(tmp_path, ["F.Cu", "B.Cu"], "1.6")
    assert (
        cli.main(["fabrication", "--brief", str(brief), "--pcb", str(good), "--out", str(out)]) == 0
    )
    assert json.loads(out.read_text(encoding="utf-8"))["artifact_kind"] == "circuit_fabrication"
    bad = _pcb(tmp_path, ["F.Cu"], "1.6")
    assert cli.main(["fabrication", "--brief", str(brief), "--pcb", str(bad)]) == 1
    capsys.readouterr()
    assert cli.main(["fabrication", "--brief", str(BRIEF), "--pcb", str(bad)]) == 1
