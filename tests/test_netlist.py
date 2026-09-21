from pathlib import Path

import pytest

from circuit.brief import load_brief
from circuit.netlist import NetlistError, check_connectivity, parse_netlist

ROOT = Path(__file__).parent


def test_parse_sample_netlist() -> None:
    parsed = parse_netlist(ROOT / "data" / "netlist_sample.net")
    assert parsed.components["R1"].footprint.startswith("Resistor_THT:")
    assert parsed.nets["LED_A"] == frozenset({("R1", "2"), ("D1", "2")})


def test_connectivity_passes() -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    netlist_path = ROOT / "data" / "netlist_sample.net"
    result = check_connectivity(
        load_brief(brief_path),
        parse_netlist(netlist_path),
        brief_path=brief_path,
        netlist_path=netlist_path,
    )
    assert result.verdict == "pass"
    assert result.missing_nets == []


@pytest.mark.parametrize(
    ("replacement", "field"),
    [
        (
            '(net (code "1") (name "VIN") (node (ref "J1") (pin "1")) (node (ref "R1") (pin "2")))',
            "mismatched_nets",
        ),
        (
            '(net (code "4") (name "OTHER") (node (ref "U1") (pin "1")) '
            '(node (ref "U1") (pin "2")))',
            "unexpected_nets",
        ),
    ],
)
def test_connectivity_detects_bad_nets(tmp_path: Path, replacement: str, field: str) -> None:
    source = (ROOT / "data" / "netlist_sample.net").read_text(encoding="utf-8")
    if field == "mismatched_nets":
        source = source.replace(
            '(net (code "1") (name "VIN")\n'
            '      (node (ref "J1") (pin "1"))\n'
            '      (node (ref "R1") (pin "1"))\n'
            "    )",
            replacement,
        )
    else:
        source = source.replace(
            "  )\n)\n",
            f"    {replacement}\n  )\n)\n",
            1,
        )
    path = tmp_path / "bad.net"
    path.write_text(source, encoding="utf-8")
    result = check_connectivity(
        load_brief(ROOT / "data" / "brief_led_loop.json"),
        parse_netlist(path),
        brief_path=ROOT / "data" / "brief_led_loop.json",
        netlist_path=path,
    )
    assert result.verdict == "fail"
    assert getattr(result, field)


def test_connectivity_detects_missing_part_and_footprint_mismatch(tmp_path: Path) -> None:
    source = (ROOT / "data" / "netlist_sample.net").read_text(encoding="utf-8")
    source = source.replace('(ref "D1")\n      (value "LED")', '(ref "D2")\n      (value "LED")')
    source = source.replace('(footprint "LED_THT:LED_D5.0mm")', '(footprint "LED_THT:Other")')
    path = tmp_path / "missing.net"
    path.write_text(source, encoding="utf-8")
    result = check_connectivity(
        load_brief(ROOT / "data" / "brief_led_loop.json"),
        parse_netlist(path),
        brief_path=ROOT / "data" / "brief_led_loop.json",
        netlist_path=path,
    )
    assert result.verdict == "fail"
    assert "D1" in result.missing_parts
    assert result.footprint_mismatches == {}


@pytest.mark.parametrize(
    "text",
    ["(root)", '(export (version "E"))', '(export (components) (nets (net (name "N")))'],
)
def test_parser_fails_closed(tmp_path: Path, text: str) -> None:
    path = tmp_path / "invalid.net"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(NetlistError):
        parse_netlist(path)
