import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from circuit import cli
from circuit.brief import DesignBrief
from circuit.dft import check_dft

DATA = Path(__file__).parent / "data"
BRIEF = DATA / "brief_desk_lamp.json"
SPEC = DATA / "prodeng-factory-test-spec.json"
RENAME = {"DBG_TX": "UART_TX", "DBG_RX": "UART_RX", "BTN_MODE": "FTM_STRAP"}


def _tp(reference: str) -> dict[str, Any]:
    return {
        "reference": reference,
        "lib_id": "Connector:TestPoint",
        "footprint": "TestPoint:TestPoint_Pad_D1.0mm",
    }


def _setup(
    tmp_path: Path, points: dict[str, str], spec: bytes | None = None, pin: str | None = None
) -> Path:
    raw = SPEC.read_bytes() if spec is None else spec
    (tmp_path / "factory-test-spec.json").write_bytes(raw)
    value: dict[str, Any] = json.loads(BRIEF.read_text(encoding="utf-8"))
    for net in value["nets"]:
        net["name"] = RENAME.get(net["name"], net["name"])
    for reference, net_name in points.items():
        value["parts"].append(_tp(reference))
        next(net for net in value["nets"] if net["name"] == net_name)["pins"].append(
            f"{reference}.1"
        )
    value["dft"] = {
        "ftm_spec_path": "factory-test-spec.json",
        "ftm_spec_sha256": pin or hashlib.sha256(raw).hexdigest(),
    }
    path = tmp_path / "desk_lamp.brief.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _report(path: Path) -> Any:
    return check_dft(DesignBrief.model_validate_json(path.read_text(encoding="utf-8")), path)


ALL = {"TP1": "UART_TX", "TP2": "UART_RX", "TP3": "FTM_STRAP"}


def test_every_factory_test_net_with_a_test_point_passes(tmp_path: Path) -> None:
    report = _report(_setup(tmp_path, ALL))
    assert report.verdict == "pass"
    assert [(c.net, c.test_points) for c in report.checks] == [
        ("FTM_STRAP", ["TP3"]),
        ("UART_RX", ["TP2"]),
        ("UART_TX", ["TP1"]),
    ]


def test_net_without_test_point_fails(tmp_path: Path) -> None:
    report = _report(_setup(tmp_path, {"TP1": "UART_TX", "TP2": "UART_RX"}))
    assert report.verdict == "fail"
    assert [c.net for c in report.checks if c.status == "fail"] == ["FTM_STRAP"]


def test_connector_pin_is_not_a_test_point(tmp_path: Path) -> None:
    report = _report(_setup(tmp_path, {}))
    assert report.verdict == "fail"
    assert {c.detail for c in report.checks} == {"no TestPoint footprint on net"}


def test_net_missing_from_brief_fails(tmp_path: Path) -> None:
    spec: dict[str, Any] = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["commands"][0]["measures_nets"].append("VBAT_SENSE")
    raw = (json.dumps(spec, indent=2, sort_keys=True) + "\n").encode("utf-8")
    report = _report(_setup(tmp_path, ALL, spec=raw))
    assert report.verdict == "fail"
    assert [(c.net, c.detail) for c in report.checks if c.status == "fail"] == [
        ("VBAT_SENSE", "net not in brief")
    ]


def test_changed_spec_fails(tmp_path: Path) -> None:
    report = _report(_setup(tmp_path, ALL, pin="0" * 64))
    assert (report.verdict, report.checks) == ("fail", [])
    assert "changed since pinned" in report.detail


@pytest.mark.parametrize("raw", [b"{", b'{"declared": true}', b"\xff"])
def test_malformed_spec_fails(tmp_path: Path, raw: bytes) -> None:
    report = _report(_setup(tmp_path, ALL, spec=raw))
    assert report.verdict == "fail"
    assert "malformed" in report.detail


def test_missing_spec_is_unknown(tmp_path: Path) -> None:
    path = _setup(tmp_path, ALL)
    (tmp_path / "factory-test-spec.json").unlink()
    assert _report(path).verdict == "unknown"


def test_undeclared_ftm_passes_with_no_nets(tmp_path: Path) -> None:
    raw = b'{"declared": false, "reason": "No factory_test_mode is declared."}\n'
    report = _report(_setup(tmp_path, {}, spec=raw))
    assert (report.verdict, report.checks) == ("pass", [])


def test_cli_exit_follows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "desk_lamp.dft.json"
    assert cli.main(["dft", "--brief", str(_setup(tmp_path, ALL)), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["artifact_kind"] == "circuit_dft"
    assert cli.main(["dft", "--brief", str(_setup(tmp_path, {}))]) == 1
    capsys.readouterr()
    assert cli.main(["dft", "--brief", str(BRIEF)]) == 1
