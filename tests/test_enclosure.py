from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from circuit import cli
from circuit._envelope import sidecar_path, write_envelope
from circuit.brief import DesignBrief
from circuit.enclosure import check_enclosure

BRIEF = Path(__file__).parent / "data" / "brief_desk_lamp.json"
SHA = "0" * 64


def _envelope(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "system": "mech",
        "artifact_kind": "mech_board_envelope",
        "frame": "board_center_y_up_mm",
        "design": "demo",
        "brief_sha256": SHA,
        "board": {
            "width_mm": 60.0,
            "depth_mm": 40.0,
            "thickness_mm": 1.6,
            "mount_holes": [{"id": "MH1", "x_mm": -25.0, "y_mm": -15.0, "diameter_mm": 3.2}],
        },
        "max_height_top_mm": 10.0,
        "max_height_bottom_mm": 2.0,
        "openings": [{"id": "O1", "face": "front", "along_mm": [-6.0, 6.0], "z_mm": [1.0, 7.0]}],
    }
    value.update(overrides)
    return value


def _part(ref: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ref": ref,
        "lib_id": "Device:R",
        "side": "top",
        "x_mm": 0.0,
        "y_mm": 0.0,
        "rotation_deg": 0.0,
        "bbox_mm": [-1.0, -1.0, 1.0, 1.0],
        "height_mm": 1.0,
        "height_sources": ["property"],
        "connector": False,
        "edge": None,
    }
    value.update(overrides)
    return value


def _geometry(parts: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "pcb": {"path": "demo.kicad_pcb", "sha256": SHA},
        "width_mm": 60.0,
        "depth_mm": 40.0,
        "thickness_mm": 1.6,
        "outline_mm": [[-30.0, -20.0], [30.0, -20.0], [30.0, 20.0], [-30.0, 20.0]],
        "mount_holes": [{"ref": "H1", "x_mm": -25.0, "y_mm": -15.0, "diameter_mm": 3.2}],
        "components": parts,
        "max_height_top_mm": max([p["height_mm"] or 0.0 for p in parts] or [0.0]),
        "max_height_bottom_mm": None,
        "unknown": [],
        "verdict": "pass",
    }
    value.update(overrides)
    return value


USB = _part(
    "J1",
    lib_id="Connector:USB_C",
    bbox_mm=[-4.75, -20.5, 4.75, -14.0],
    height_mm=3.5,
    connector=True,
    edge="front",
)


def _setup(
    tmp_path: Path,
    parts: list[dict[str, Any]] | None = None,
    envelope: bytes | None = None,
    pin: str | None = None,
    geometry: dict[str, Any] | None = None,
) -> tuple[DesignBrief, Path, Path]:
    raw = json.dumps(_envelope()).encode() if envelope is None else envelope
    (tmp_path / "demo.mech-board-envelope.json").write_bytes(raw)
    write_envelope(tmp_path / "demo.mech-board-envelope.json", "mech", [], tmp_path)
    value: dict[str, Any] = json.loads(BRIEF.read_text(encoding="utf-8"))
    value["enclosure"] = {
        "envelope_path": "demo.mech-board-envelope.json",
        "envelope_sha256": pin or hashlib.sha256(raw).hexdigest(),
    }
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(value), encoding="utf-8")
    geometry_path = tmp_path / "demo.board-geometry.json"
    data = geometry if geometry is not None else _geometry(parts if parts is not None else [USB])
    geometry_path.write_text(json.dumps(data), encoding="utf-8")
    return DesignBrief.model_validate(value), brief_path, geometry_path


def _statuses(report: Any) -> dict[str, str]:
    return {check.subject: check.status for check in report.checks}


def test_matching_board_and_served_connector_pass(tmp_path: Path) -> None:
    report = check_enclosure(*_setup(tmp_path))
    assert report.verdict == "pass", report
    assert _statuses(report)["connector:J1"] == "pass"
    assert report.geometry_sha256 is not None


@pytest.mark.parametrize(("height", "status"), [(9.99, "pass"), (10.0, "pass"), (10.01, "fail")])
def test_top_height_limit_boundary(tmp_path: Path, height: float, status: str) -> None:
    report = check_enclosure(*_setup(tmp_path, [USB, _part("C1", height_mm=height)]))
    assert _statuses(report)["height:C1"] == status


def test_bottom_part_uses_the_bottom_limit(tmp_path: Path) -> None:
    report = check_enclosure(*_setup(tmp_path, [_part("C2", side="bottom", height_mm=2.5)]))
    assert _statuses(report)["height:C2"] == "fail"
    assert report.verdict == "fail"


def test_unknown_part_height_is_unknown(tmp_path: Path) -> None:
    report = check_enclosure(*_setup(tmp_path, [_part("U1", height_mm=None)]))
    assert report.verdict == "unknown"


@pytest.mark.parametrize(
    "opening",
    [
        {"id": "O1", "face": "back", "along_mm": [-6.0, 6.0], "z_mm": [1.0, 7.0]},
        {"id": "O1", "face": "front", "along_mm": [-3.0, 6.0], "z_mm": [1.0, 7.0]},
        {"id": "O1", "face": "front", "along_mm": [-6.0, 6.0], "z_mm": [4.0, 7.0]},
    ],
    ids=["wrong-face", "too-narrow", "above-connector"],
)
def test_connector_without_an_opening_fails(tmp_path: Path, opening: dict[str, Any]) -> None:
    raw = json.dumps(_envelope(openings=[opening])).encode()
    report = check_enclosure(*_setup(tmp_path, envelope=raw))
    assert _statuses(report)["connector:J1"] == "fail"
    assert report.verdict == "fail"


def test_board_size_and_holes_must_match(tmp_path: Path) -> None:
    geometry = _geometry(
        [USB],
        width_mm=60.1,
        mount_holes=[{"ref": "H1", "x_mm": -24.0, "y_mm": -15.0, "diameter_mm": 3.2}],
    )
    report = check_enclosure(*_setup(tmp_path, geometry=geometry))
    statuses = _statuses(report)
    assert statuses["width"] == "fail"
    assert statuses["mount_holes"] == "fail"
    assert statuses["depth"] == "pass"


def test_changed_envelope_fails(tmp_path: Path) -> None:
    report = check_enclosure(*_setup(tmp_path, pin="f" * 64))
    assert report.verdict == "fail"
    assert "changed since pinned" in report.detail


@pytest.mark.parametrize(
    "raw",
    [b"{", b"\xff", json.dumps(_envelope(frame="board_corner")).encode()],
    ids=["json", "utf8", "schema"],
)
def test_malformed_envelope_fails(tmp_path: Path, raw: bytes) -> None:
    report = check_enclosure(*_setup(tmp_path, envelope=raw))
    assert report.verdict == "fail"
    assert "malformed" in report.detail


def test_missing_envelope_is_unknown(tmp_path: Path) -> None:
    design, brief_path, geometry_path = _setup(tmp_path)
    (tmp_path / "demo.mech-board-envelope.json").unlink()
    assert check_enclosure(design, brief_path, geometry_path).verdict == "unknown"


def test_incomplete_or_missing_geometry_is_unknown(tmp_path: Path) -> None:
    geometry = _geometry([USB], verdict="fail", unknown=["outline"])
    design, brief_path, geometry_path = _setup(tmp_path, geometry=geometry)
    assert check_enclosure(design, brief_path, geometry_path).verdict == "unknown"
    geometry_path.unlink()
    assert check_enclosure(design, brief_path, geometry_path).verdict == "unknown"


def test_cli_writes_report_and_rejects_brief_without_enclosure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, brief_path, geometry_path = _setup(tmp_path)
    out = tmp_path / "enclosure.json"
    cli.main(
        [
            "enclosure",
            "--brief",
            str(brief_path),
            "--geometry",
            str(geometry_path),
            "--out",
            str(out),
        ]
    )
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "pass"
    capsys.readouterr()
    cli.main(["enclosure", "--brief", str(BRIEF), "--geometry", str(geometry_path)])
    assert "no enclosure section" in capsys.readouterr().out


def test_mech_envelope_provenance_fails_closed(tmp_path: Path) -> None:
    design, brief_path, geometry_path = _setup(tmp_path)
    sidecar = sidecar_path(tmp_path / "demo.mech-board-envelope.json")
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    sidecar.write_text(json.dumps(body | {"producer": "wire"}), encoding="utf-8")
    assert (
        _statuses(check_enclosure(design, brief_path, geometry_path))["import_provenance"] == "fail"
    )
    sidecar.unlink()
    report = check_enclosure(design, brief_path, geometry_path)
    assert (_statuses(report)["import_provenance"], report.verdict) == ("unknown", "unknown")
