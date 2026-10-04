from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from circuit.pinsource import (
    PinSource,
    PinSourceError,
    PinSourceKind,
    PinSourcePin,
    compare_pin_sources,
    parse_bsdl,
    parse_ibis,
    parse_pin_source,
)


def _source(
    kind: PinSourceKind,
    rows: list[tuple[str, str]],
    digest: str,
    *,
    identity: list[str] | None = None,
    derived_from: list[str] | None = None,
) -> PinSource:
    return PinSource(
        kind=kind,
        lineage=kind,
        description=f"{kind} fixture",
        sha256=digest,
        identity=identity or ["fixture-device"],
        derived_from=derived_from or [],
        pins=[PinSourcePin(number=number, name=name) for number, name in rows],
    )


def test_parse_ibis_pin_section_and_ignore_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "part.ibs"
    content = (
        "[IBIS Ver] 7.1\n"
        "[Pin]\n"
        "| pin signal model\n"
        "1 VDD POWER\n"
        "2 GND GND_MODEL\n"
        "[Package]\n"
        "R_pkg 0.1 0.1 0.1\n"
    )
    path.write_text(content, encoding="utf-8")

    source = parse_ibis(path)

    assert [(pin.number, pin.name) for pin in source.pins] == [
        ("1", "VDD"),
        ("2", "GND"),
    ]
    assert source.kind == "ibis"
    assert source.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert parse_pin_source(path) == source


def test_parse_bsdl_pin_map_across_string_fragments(tmp_path: Path) -> None:
    path = tmp_path / "part.bsdl"
    content = 'constant PIN_MAP_STRING : PIN_MAP_STRING := "1 : VDD, 2 : GND, " & "3 : NC";\n'
    path.write_text(content, encoding="utf-8")

    source = parse_bsdl(path)

    assert [(pin.number, pin.name) for pin in source.pins] == [
        ("1", "VDD"),
        ("2", "GND"),
        ("3", "NC"),
    ]
    assert source.kind == "bsdl"
    assert source.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert parse_pin_source(path) == source


def test_parse_stm32_open_pin_data_preserves_bga_positions(tmp_path: Path) -> None:
    path = tmp_path / "stm32-pins.xml"
    path.write_text(
        '<Mcu RefName="STM32F103C8" Package="LQFP48">'
        '<Pin Name="PA0" Position="A1" Type="I/O"/>'
        '<Pin Name="VDD" Position="B2" Type="Power"/>'
        "</Mcu>",
        encoding="utf-8",
    )

    source = parse_pin_source(path)

    assert source.kind == "stm32_open_pin_data"
    assert source.lineage == "stm32_open_pin_data"
    assert source.identity == ["STM32F103C8", "LQFP48"]
    assert [(pin.number, pin.name) for pin in source.pins] == [
        ("A1", "PA0"),
        ("B2", "VDD"),
    ]


def test_parse_amd_package_file_skips_headers_and_retains_bank(tmp_path: Path) -> None:
    path = tmp_path / "package.csv"
    path.write_text(
        "Device: XC7A35T\n"
        "Package: CPG236\n"
        "Pin,Pin Name,Memory Byte Group,Bank,I/O Type\n"
        "A1,IO_L1P_T0_34,byte0,14,LVCMOS33\n"
        "B2,IO_L1N_T0_34,byte0,14,LVCMOS33\n"
        "Page 1 of 1,,,,\n",
        encoding="utf-8",
    )

    source = parse_pin_source(path)

    assert source.kind == "amd_package_file"
    assert source.identity == ["XC7A35T", "CPG236"]
    assert [(pin.number, pin.name, pin.bank) for pin in source.pins] == [
        ("A1", "IO_L1P_T0_34", "14"),
        ("B2", "IO_L1N_T0_34", "14"),
    ]


def test_parse_amd_pipe_delimited_text_pinout(tmp_path: Path) -> None:
    path = tmp_path / "package.txt"
    path.write_text(
        "Device: XC7A35T\n"
        "Package: CPG236\n"
        "Pin | Pin Name | Memory Byte Group | Bank | I/O Type\n"
        "A1 | IO_L1P_T0_34 | byte0 | 14 | LVCMOS33\n"
        "B2 | IO_L1N_T0_34 | byte0 | 14 | LVCMOS33\n"
        "End of package pinout\n",
        encoding="utf-8",
    )

    source = parse_pin_source(path)

    assert source.kind == "amd_package_file"
    assert [(pin.number, pin.name, pin.bank) for pin in source.pins] == [
        ("A1", "IO_L1P_T0_34", "14"),
        ("B2", "IO_L1N_T0_34", "14"),
    ]


def test_parse_microchip_atdf_selects_named_pinout(tmp_path: Path) -> None:
    path = tmp_path / "device.atdf"
    path.write_text(
        "<avr-tools-device-file><devices><device name='ATmega324PA'>"
        "<pinouts><pinout name='TQFP32'>"
        "<pin position='1' pad='PA0'/>"
        "</pinout><pinout name='QFN32'>"
        "<pin position='A1' pad='PA1'/>"
        "</pinout></pinouts></device></devices></avr-tools-device-file>",
        encoding="utf-8",
    )

    source = parse_pin_source(path, pinout_name="QFN32")

    assert source.kind == "microchip_atdf"
    assert source.identity == ["ATmega324PA", "QFN32"]
    assert [(pin.number, pin.name) for pin in source.pins] == [("A1", "PA1")]


def test_microchip_atdf_requires_a_named_pinout(tmp_path: Path) -> None:
    path = tmp_path / "device.atdf"
    path.write_text(
        "<device name='ATmega324PA'><pinouts><pinout name='QFN32'>"
        "<pin position='A1' pad='PA1'/></pinout></pinouts></device>",
        encoding="utf-8",
    )

    with pytest.raises(PinSourceError, match="requires a pinout name"):
        parse_pin_source(path)


def test_compare_pin_sources_uses_pinout_name_normalization() -> None:
    class_a = _source(
        "part_spec",
        [("1", "V_DD"), ("2", "GND"), ("3", "NC")],
        "a" * 64,
    )
    class_b = _source(
        "ibis",
        [("1", "VDD"), ("2", "VSS"), ("4", "RESET")],
        "b" * 64,
    )

    result = compare_pin_sources(class_a, class_b)

    assert not result.passed
    assert [(item.code, item.number) for item in result.findings] == [
        ("pin_source_name_mismatch", "2"),
        ("pin_source_missing", "3"),
        ("pin_source_missing", "4"),
    ]


def test_pin_source_lineages_are_deduplicated_and_identity_is_checked() -> None:
    class_a = _source("part_spec", [("1", "VDD")], "a" * 64, identity=["STM32F103C8"])
    independent = _source(
        "stm32_open_pin_data",
        [("1", "VDD")],
        "b" * 64,
        identity=["STM32F103"],
    )
    duplicate_lineage = _source(
        "stm32_open_pin_data",
        [("1", "VDD")],
        "e" * 64,
        identity=["STM32F103C8"],
    )
    derived = _source(
        "amd_package_file",
        [("1", "VDD")],
        "c" * 64,
        identity=["STM32F103C8"],
        derived_from=["stm32_open_pin_data"],
    )
    orphan_derived = _source(
        "amd_package_file",
        [("1", "VDD")],
        "8" * 64,
        identity=["STM32F103C8"],
        derived_from=["uncompared_lineage"],
    )

    matched = compare_pin_sources(class_a, [independent, duplicate_lineage, derived])
    orphan_lineage = compare_pin_sources(class_a, orphan_derived)
    mismatched = compare_pin_sources(
        class_a,
        _source("bsdl", [("1", "VDD")], "d" * 64, identity=["OTHER-DEVICE"]),
    )
    short_prefix = compare_pin_sources(
        _source("part_spec", [("1", "VDD")], "e" * 64, identity=["RGT0016C"]),
        _source("amd_package_file", [("1", "VDD")], "f" * 64, identity=["RGT"]),
    )

    assert matched.independent_lineages == ["part_spec", "stm32_open_pin_data"]
    assert not matched.findings
    assert orphan_lineage.independent_lineages == ["part_spec"]
    assert [item.code for item in mismatched.findings] == ["pin_source_identity_mismatch"]
    assert [item.code for item in short_prefix.findings] == ["pin_source_identity_mismatch"]


@pytest.mark.parametrize(
    ("spec_identity", "source_identity", "compatible"),
    [
        ("ABC-123", "ABC123", True),
        ("RGT0016", "RGT", True),
        ("RGT0016C", "RGT", False),
        ("STM32F407VGT6", "STM32F", False),
    ],
)
def test_identity_prefix_matching_limits_suffix_length(
    spec_identity: str,
    source_identity: str,
    compatible: bool,
) -> None:
    spec_source = _source(
        "part_spec",
        [("1", "VDD")],
        "a" * 64,
        identity=[spec_identity],
    )
    pin_source = _source(
        "amd_package_file",
        [("1", "VDD")],
        "b" * 64,
        identity=[source_identity],
    )

    result = compare_pin_sources(spec_source, pin_source)

    assert (
        "pin_source_identity_mismatch" not in [item.code for item in result.findings]
    ) is compatible


@pytest.mark.parametrize(
    ("mpn", "compatible"),
    [
        ("STM32F407VGT6", True),
        ("STM32F407VET6", True),
        ("STM32F407ZGT6", False),
        ("STM32F405VGT6", False),
        ("STM32F407VGT6TR", True),
    ],
)
def test_stm32_refname_choice_groups_and_wildcards_match_mpn(
    mpn: str,
    compatible: bool,
) -> None:
    spec_source = _source(
        "part_spec",
        [("1", "VDD")],
        "a" * 64,
        identity=[mpn],
    )
    stm32_source = _source(
        "stm32_open_pin_data",
        [("1", "VDD")],
        "b" * 64,
        identity=["STM32F407V(E-G)Tx"],
    )

    result = compare_pin_sources(spec_source, stm32_source)

    assert (
        "pin_source_identity_mismatch" not in [item.code for item in result.findings]
    ) is compatible


@pytest.mark.parametrize(
    ("suffix", "content", "message"),
    [
        (".ibs", "[Pin]\n1\n", "invalid IBIS [Pin] row"),
        (".bsdl", "entity test is end;", "no PIN_MAP_STRING"),
        (".unknown", "data", "unsupported pin source extension"),
    ],
)
def test_malformed_and_unsupported_pin_sources_fail_closed(
    tmp_path: Path,
    suffix: str,
    content: str,
    message: str,
) -> None:
    path = tmp_path / f"part{suffix}"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PinSourceError, match=re.escape(message)):
        parse_pin_source(path)


def test_duplicate_pin_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate pin numbers"):
        _source("ibis", [("1", "VDD"), ("1", "GND")], "c" * 64)
