"""Contract tests for the wire-agent ConnectivitySource emitter."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit.brief import DesignBrief
from circuit.connectivity import (
    ConnectivityError,
    connectivity_source,
    write_connectivity,
)
from circuit.netlist import parse_netlist

FIXTURE = Path(__file__).parent / "fixtures" / "upstream" / "board.connectivity.json"
SIGNAL_CLASSES = {"power", "ground", "signal", "analog", "data", "highspeed", "shield"}
ANCHOR_KINDS = {"clip", "grommet", "breakout", "other"}


def _assert_connectivity_shape(payload: dict[str, Any]) -> None:
    """Mirror of wire's ConnectivitySource importer (extra=forbid)."""
    assert payload["schema_version"] == 1
    assert payload["system"] == "circuit"
    allowed_connector = {
        "ref",
        "family_hint",
        "housing",
        "rated_current_a",
        "rated_voltage_v",
        "cavities",
    }
    for connector in payload["connectors"]:
        assert set(connector) <= allowed_connector
        assert connector["ref"]
        # wire SourceConnector defaults rated_current_a=3.0/rated_voltage_v=250.0
        assert connector.get("rated_current_a", 3.0) > 0
        assert connector.get("rated_voltage_v", 250.0) > 0
        assert len(connector["cavities"]) >= 1
    allowed_net = {"ref", "signal_class", "voltage_v", "current_a"}
    for net in payload["nets"]:
        assert set(net) == allowed_net
        assert net["signal_class"] in SIGNAL_CLASSES
        assert net["voltage_v"] >= 0
        assert net["current_a"] >= 0


def _brief(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "demo",
        "parts": [
            {
                "reference": "J1",
                "lib_id": "Connector_Generic:Conn_01x04",
                "footprint": "Conn:PinHeader_1x04",
                "connector": True,
                "housing": "B4B-XH-A",
                "rated_current_a": 3.0,
                "rated_voltage_v": 250.0,
            },
            {
                "reference": "J2",
                "lib_id": "Connector_Generic:Conn_01x02",
                "footprint": "Conn:PinHeader_1x02",
                "connector": True,
                "housing": "B2B-XH-A",
            },
            {
                "reference": "U1",
                "lib_id": "MCU:ATSAMD21",
                "footprint": "QFP:TQFP-48",
            },
        ],
        "nets": [
            {
                "name": "VCC",
                "pins": ["J1.1", "U1.1"],
                "signal_class": "power",
                "voltage_v": 12.0,
                "current_a": 1.5,
            },
            {"name": "GND", "pins": ["J1.2", "J2.1", "U1.2"], "voltage_v": 0.0, "current_a": 1.5},
            {
                "name": "SDA",
                "pins": ["J1.3", "U1.3"],
                "signal_class": "data",
                "voltage_v": 3.3,
                "current_a": 0.05,
            },
            {"name": "SCL", "pins": ["J1.4", "J2.2", "U1.4"], "voltage_v": 3.3, "current_a": 0.05},
        ],
        "board": {"width_mm": 50.0, "height_mm": 40.0},
    }
    data.update(overrides)
    return data


def test_connectivity_source_emits_contract() -> None:
    brief = DesignBrief.model_validate(_brief())
    payload = connectivity_source(brief)
    _assert_connectivity_shape(payload)
    assert [c["ref"] for c in payload["connectors"]] == ["J1", "J2"]
    j1 = payload["connectors"][0]
    assert j1["family_hint"] == "Connector_Generic:Conn_01x04"
    assert j1["housing"] == "B4B-XH-A"
    assert j1["cavities"] == ["1", "2", "3", "4"]
    # ratings omitted fall back to wire-side defaults
    assert "rated_voltage_v" not in payload["connectors"][1]
    assert {n["ref"]: n["signal_class"] for n in payload["nets"]} == {
        "VCC": "power",
        "GND": "ground",
        "SDA": "data",
        "SCL": "signal",
    }


def test_signal_class_inference() -> None:
    brief = DesignBrief.model_validate(_brief())
    payload = connectivity_source(brief)
    classes = {n["ref"]: n["signal_class"] for n in payload["nets"]}
    assert classes["GND"] == "ground"
    assert classes["VCC"] == "power"
    assert classes["SCL"] == "signal"


def test_connector_without_pins_fails() -> None:
    data = _brief()
    data["parts"].append(
        {
            "reference": "J9",
            "lib_id": "Connector_Generic:Conn_01x01",
            "footprint": "Conn:PinHeader_1x01",
            "connector": True,
        }
    )
    brief = DesignBrief.model_validate(data)
    with pytest.raises(ConnectivityError, match="J9"):
        connectivity_source(brief)


def test_golden_fixture_matches_shape() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _assert_connectivity_shape(payload)
    assert [c["ref"] for c in payload["connectors"]] == ["J1", "J2"]


def test_write_connectivity_roundtrip(tmp_path: Path) -> None:
    brief = DesignBrief.model_validate(_brief())
    out = tmp_path / "board.connectivity-source.json"
    write_connectivity(brief, out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    _assert_connectivity_shape(payload)


def test_cli_export(tmp_path: Path) -> None:
    brief_path = tmp_path / "demo.brief.json"
    brief_path.write_text(json.dumps(_brief()), encoding="utf-8")
    out_path = tmp_path / "demo.connectivity-source.json"
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit.connectivity",
            "--brief",
            str(brief_path),
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    status = json.loads(proc.stdout)
    assert status["verdict"] == "pass"
    _assert_connectivity_shape(json.loads(out_path.read_text(encoding="utf-8")))


def test_cli_export_bad_brief_fails_closed(tmp_path: Path) -> None:
    brief_path = tmp_path / "bad.brief.json"
    brief_path.write_text('{"name": "bad"}', encoding="utf-8")
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit.connectivity",
            "--brief",
            str(brief_path),
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 1
    status = json.loads(proc.stdout)
    assert status["verdict"] == "fail"
    assert status["stage"] == "connectivity-export"


def test_brief_rejects_extra_connector_fields() -> None:
    data = _brief()
    data["parts"][0]["bogus"] = 1
    with pytest.raises(ValidationError):
        DesignBrief.model_validate(data)


def test_netlist_drives_cavities(tmp_path: Path) -> None:
    netlist_text = """(export
  (components
    (comp (ref J1) (value X) (footprint F)))
  (nets
    (net (name GND) (node (ref J1) (pin 2)) (node (ref U1) (pin 1)))
    (net (name VCC) (node (ref J1) (pin 5)) (node (ref U1) (pin 2)))))
"""
    netlist_path = tmp_path / "board.net"
    netlist_path.write_text(netlist_text, encoding="utf-8")
    data = _brief()
    data["parts"] = [part for part in data["parts"] if part["reference"] != "J2"]
    for net in data["nets"]:
        net["pins"] = [pin for pin in net["pins"] if not pin.startswith("J2.")]
    brief = DesignBrief.model_validate(data)
    payload = connectivity_source(brief, parse_netlist(netlist_path))
    assert payload["connectors"][0]["cavities"] == ["2", "5"]

    # a connector declared in the brief but absent from the netlist fails closed
    brief = DesignBrief.model_validate(_brief())
    with pytest.raises(ConnectivityError, match="J2"):
        connectivity_source(brief, parse_netlist(netlist_path))
