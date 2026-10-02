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


def _source(kind: PinSourceKind, rows: list[tuple[str, str]], digest: str) -> PinSource:
    return PinSource(
        kind=kind,
        description=f"{kind} fixture",
        sha256=digest,
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
