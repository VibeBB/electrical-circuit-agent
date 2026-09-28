"""Firmware link: MCU connectivity export and firmware pin map check."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit import cli
from circuit.brief import DesignBrief, load_brief
from circuit.firmware import (
    CircuitFirmwareConnectivity,
    FirmwareLinkError,
    FirmwarePinmap,
    check_firmware_pinmap,
    firmware_connectivity,
    pad_of,
    write_firmware_connectivity,
)
from circuit.netlist import parse_netlist

DATA = Path(__file__).parent / "data"
BRIEF = DATA / "brief_desk_lamp.json"
NETLIST = DATA / "netlist_desk_lamp.net"
SHA = "0" * 64


def _entry(signal: str, pad: str, net: str, function: str) -> dict[str, Any]:
    return {
        "signal": signal,
        "pad": pad,
        "pad_aliases": [f"IO{pad[4:]}", *{"GPIO43": ["TXD0"], "GPIO44": ["RXD0"]}.get(pad, [])],
        "package_pin": None,
        "net": net,
        "function": function,
        "peripheral": None,
    }


def _pinmap() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "system": "firmware",
        "artifact_kind": "firmware_pinmap",
        "design": "desk-lamp",
        "contract_sha256": SHA,
        "mcu_ref": "U1",
        "mcu_profile": "esp32s3",
        "mcu_part": "ESP32-S3",
        "io_voltage_max_v": 3.6,
        "supply_net": "+3V3",
        "pins": [
            _entry("mode_button", "GPIO4", "BTN_MODE", "gpio_in"),
            _entry("lamp_led", "GPIO5", "LED_PWM", "pwm"),
            _entry("debug_tx", "GPIO43", "DBG_TX", "uart_tx"),
            _entry("debug_rx", "GPIO44", "DBG_RX", "uart_rx"),
        ],
        "free_pads": [
            {"pad": "GPIO6", "pad_aliases": ["IO6"], "package_pin": None},
            {"pad": "GPIO7", "pad_aliases": ["IO7"], "package_pin": None},
        ],
    }


def _connectivity(netlist: bool = True) -> CircuitFirmwareConnectivity:
    design = load_brief(BRIEF)
    if netlist:
        return firmware_connectivity(design, BRIEF, parse_netlist(NETLIST), NETLIST)
    return firmware_connectivity(design, BRIEF)


def test_netlist_parser_keeps_pin_functions() -> None:
    parsed = parse_netlist(NETLIST)
    assert parsed.pin_functions["U1.4"] == "IO4"
    assert parsed.pin_functions["U1.37"] == "TXD0"


def test_export_from_netlist_carries_functions_and_hashes(tmp_path: Path) -> None:
    payload = _connectivity()
    assert payload.source == "netlist"
    assert payload.netlist_sha256 is not None
    (mcu,) = payload.mcus
    assert mcu.ref == "U1"
    pins = {p.pin: p for p in mcu.pins}
    assert pins["4"].function == "IO4"
    assert pins["4"].net == "BTN_MODE"
    assert pins["2"].signal_class == "power"
    assert [p.pin for p in mcu.pins] == sorted((p.pin for p in mcu.pins), key=int)
    out = write_firmware_connectivity(payload, tmp_path / "desk-lamp.firmware.json")
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["artifact_kind"] == "circuit_firmware_connectivity"
    assert written["system"] == "circuit"


def test_export_from_brief_has_no_functions() -> None:
    payload = _connectivity(netlist=False)
    assert payload.source == "brief"
    assert payload.netlist_sha256 is None
    assert all(p.function is None for p in payload.mcus[0].pins)


def test_export_requires_an_mcu_part() -> None:
    data = json.loads(BRIEF.read_text(encoding="utf-8"))
    for part in data["parts"]:
        part.pop("mcu", None)
    with pytest.raises(FirmwareLinkError, match="mcu"):
        firmware_connectivity(DesignBrief.model_validate(data), BRIEF)


def test_export_fails_when_mcu_missing_from_netlist(tmp_path: Path) -> None:
    text = NETLIST.read_text(encoding="utf-8").replace('(ref "U1")', '(ref "U9")')
    netlist = tmp_path / "renamed.net"
    netlist.write_text(text, encoding="utf-8")
    with pytest.raises(FirmwareLinkError, match="U1 is not in the netlist"):
        firmware_connectivity(load_brief(BRIEF), BRIEF, parse_netlist(netlist), netlist)


def test_pad_of_normalises_kicad_functions() -> None:
    assert pad_of("GPIO26_ADC0") == "GPIO26"
    assert pad_of("IO5") == "IO5"
    assert pad_of("GPIO5/TOUCH5") == "GPIO5"
    assert pad_of(None) is None


def test_matching_pinmap_passes() -> None:
    report = check_firmware_pinmap(_connectivity(), FirmwarePinmap.model_validate(_pinmap()), SHA)
    assert report.verdict == "pass", report.problems
    assert "mode_button=U1.4/BTN_MODE" in report.matched
    assert report.firmware_contract_sha256 == SHA


def test_brief_source_resolves_by_package_pin() -> None:
    data = _pinmap()
    for entry, pin in zip(data["pins"], ("4", "5", "37", "36"), strict=True):
        entry["package_pin"] = pin
    report = check_firmware_pinmap(
        _connectivity(netlist=False), FirmwarePinmap.model_validate(data), SHA
    )
    assert report.verdict == "pass", report.problems


def test_brief_source_without_package_pins_fails_closed() -> None:
    report = check_firmware_pinmap(
        _connectivity(netlist=False), FirmwarePinmap.model_validate(_pinmap()), SHA
    )
    assert report.verdict == "fail"
    assert any("is not connected" in problem for problem in report.problems)


def _wrong_net(d: dict[str, Any]) -> None:
    d["pins"][0]["net"] = "BTN_OTHER"


def _unwired_pad(d: dict[str, Any]) -> None:
    d["pins"][1].update(pad="GPIO9", pad_aliases=["IO9"])


def _unknown_mcu(d: dict[str, Any]) -> None:
    d["mcu_ref"] = "U7"


def _unknown_supply(d: dict[str, Any]) -> None:
    d["supply_net"] = "+1V8"


def _low_io_limit(d: dict[str, Any]) -> None:
    d["io_voltage_max_v"] = 3.0


def _free_driven_pad(d: dict[str, Any]) -> None:
    d["free_pads"].append({"pad": "CHIP_PU", "pad_aliases": ["EN"], "package_pin": None})


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_wrong_net, "firmware expects BTN_OTHER"),
        (_unwired_pad, "not connected"),
        (_unknown_mcu, "circuit has no MCU U7"),
        (_unknown_supply, "supply net +1V8"),
        (_low_io_limit, "exceeds ESP32-S3 I/O maximum"),
        (_free_driven_pad, "leaves the pad unassigned"),
    ],
)
def test_mismatches_fail(mutate: Callable[[dict[str, Any]], None], needle: str) -> None:
    data = copy.deepcopy(_pinmap())
    mutate(data)
    report = check_firmware_pinmap(_connectivity(), FirmwarePinmap.model_validate(data), SHA)
    assert report.verdict == "fail"
    assert any(needle in problem for problem in report.problems), report.problems


def test_power_net_assignment_fails() -> None:
    data = _pinmap()
    data["pins"].append(
        {**_entry("supply", "GPIO6", "+3V3", "gpio_in"), "package_pin": "2", "pad_aliases": []}
    )
    report = check_firmware_pinmap(_connectivity(), FirmwarePinmap.model_validate(data), SHA)
    assert report.verdict == "fail"
    assert any("is a power net" in problem for problem in report.problems)


def test_pinmap_schema_is_strict() -> None:
    data = _pinmap()
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        FirmwarePinmap.model_validate(data)
    data = _pinmap()
    data["artifact_kind"] = "firmware_contract"
    with pytest.raises(ValidationError):
        FirmwarePinmap.model_validate(data)


def test_cli_export_and_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "desk-lamp.firmware.json"
    code = cli.main(
        ["firmware-export", "--brief", str(BRIEF), "--netlist", str(NETLIST), "--out", str(out)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mcus"] == ["U1"]
    assert out.is_file()

    pinmap = tmp_path / "desk-lamp.fw-pinmap.json"
    pinmap.write_text(json.dumps(_pinmap()), encoding="utf-8")
    report = tmp_path / "check.json"
    argv = ["firmware-check", "--brief", str(BRIEF), "--netlist", str(NETLIST)]
    code = cli.main([*argv, "--pinmap", str(pinmap), "--out", str(report)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["verdict"] == "pass"
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "pass"

    bad = _pinmap()
    bad["pins"][0]["net"] = "WRONG"
    pinmap.write_text(json.dumps(bad), encoding="utf-8")
    assert cli.main([*argv, "--pinmap", str(pinmap)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"


def test_cli_check_malformed_pinmap_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinmap = tmp_path / "broken.fw-pinmap.json"
    pinmap.write_text("{not json", encoding="utf-8")
    code = cli.main(["firmware-check", "--brief", str(BRIEF), "--pinmap", str(pinmap)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["verdict"] == "fail"
    assert payload["stage"] == "firmware-check"
