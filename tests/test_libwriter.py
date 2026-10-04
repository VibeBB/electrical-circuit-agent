from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from circuit import libwriter, sexpr
from circuit.landpattern import compute_land_pattern
from circuit.libitems import parse_footprint, parse_symbol
from circuit.libverify import verify_library_part
from circuit.partspec import PartSpec, PinSpec, load_part_spec, part_spec_sha256
from test_libverify import _vqfn_spec, _write_case  # pyright: ignore[reportPrivateUsage]


def _stored_spec(tmp_path: Path, spec: PartSpec | None = None) -> tuple[PartSpec, Path, str]:
    path = tmp_path / "part.spec.json"
    path.write_text(
        (spec or _vqfn_spec()).model_dump_json(indent=2),
        encoding="utf-8",
    )
    loaded = load_part_spec(path)
    return loaded, path, part_spec_sha256(path)


def test_writers_are_byte_identical_and_parse_round_trip(tmp_path: Path) -> None:
    spec, _spec_path, spec_sha256 = _stored_spec(tmp_path)
    land = compute_land_pattern(spec)
    first_footprint = tmp_path / "one" / "VQFN.kicad_mod"
    second_footprint = tmp_path / "two" / "VQFN.kicad_mod"
    first_result = libwriter.write_footprint(
        spec,
        land,
        first_footprint,
        spec_sha256=spec_sha256,
        ep_paste_margin_mm=-0.15,
    )
    second_result = libwriter.write_footprint(
        spec,
        land,
        second_footprint,
        spec_sha256=spec_sha256,
        ep_paste_margin_mm=-0.15,
    )
    assert first_footprint.read_bytes() == second_footprint.read_bytes()
    parsed_footprint = parse_footprint(first_footprint)
    assert parsed_footprint.name == spec.package.drawing_id
    assert parsed_footprint.properties["circuit_part_spec_sha256"] == spec_sha256
    assert (
        parsed_footprint.properties["circuit_land_pattern_sha256"]
        == first_result.land_pattern_sha256
    )
    assert parsed_footprint.properties["circuit_writer_version"] == libwriter.WRITER_VERSION
    assert parsed_footprint.properties["Datasheet"] == (spec.datasheet.url or spec.datasheet.path)
    assert next(pad for pad in parsed_footprint.pads if pad.number == "17").paste_margin == -0.15
    assert first_result.sha256 == hashlib.sha256(first_footprint.read_bytes()).hexdigest()
    assert first_result.sha256 == second_result.sha256

    first_symbol = tmp_path / "one" / "Fixture.kicad_sym"
    second_symbol = tmp_path / "two" / "Fixture.kicad_sym"
    libwriter.write_symbol(
        spec,
        first_symbol,
        spec_sha256=spec_sha256,
        footprint_id=f"Fixture:{spec.package.drawing_id}",
    )
    libwriter.write_symbol(
        spec,
        second_symbol,
        spec_sha256=spec_sha256,
        footprint_id=f"Fixture:{spec.package.drawing_id}",
    )
    assert first_symbol.read_bytes() == second_symbol.read_bytes()
    parsed_symbol = parse_symbol(first_symbol, spec.mpn)
    assert len(parsed_symbol.pins) == len(spec.pins)
    assert parsed_symbol.properties["Footprint"] == f"Fixture:{spec.package.drawing_id}"
    assert parsed_symbol.properties["Datasheet"] == (spec.datasheet.url or spec.datasheet.path)
    assert parsed_symbol.properties["circuit_part_spec_sha256"] == spec_sha256
    assert parsed_symbol.properties["circuit_writer_version"] == libwriter.WRITER_VERSION
    symbol_root = sexpr.parse_text(first_symbol.read_text(encoding="utf-8"))
    symbol_node = next(
        node for node in symbol_root[1:] if isinstance(node, list) and node[0] == "symbol"
    )
    datasheet_property = next(
        node
        for node in symbol_node[1:]
        if isinstance(node, list) and node[:2] == ["property", "Datasheet"]
    )
    effects = next(
        node for node in datasheet_property if isinstance(node, list) and node[0] == "effects"
    )
    assert ["hide", "yes"] in effects


def test_writer_refuses_unchecked_specs_and_unsupported_families(tmp_path: Path) -> None:
    spec, _spec_path, spec_sha256 = _stored_spec(tmp_path)
    land = compute_land_pattern(spec)
    changed = spec.model_copy(update={"mpn": "CHANGED"})
    with pytest.raises(libwriter.LibWriterError, match="part_spec_unchecked"):
        libwriter.write_symbol(
            changed,
            tmp_path / "unchecked.kicad_sym",
            spec_sha256=spec_sha256,
            footprint_id="Fixture:VQFN",
        )

    for family in ("custom", "through_hole_inline"):
        unsupported = spec.model_copy(
            update={
                "package": spec.package.model_copy(update={"family": family, "pins_per_side": None})
            }
        )
        unsupported_path = tmp_path / f"{family}.spec.json"
        unsupported_path.write_text(unsupported.model_dump_json(indent=2), encoding="utf-8")
        loaded_unsupported = load_part_spec(unsupported_path)
        with pytest.raises(libwriter.LibWriterError, match="unsupported_family"):
            libwriter.write_footprint(
                loaded_unsupported,
                land,
                tmp_path / f"{family}.kicad_mod",
                spec_sha256=part_spec_sha256(unsupported_path),
            )


def test_symbol_writer_places_pins_on_sides_by_role(tmp_path: Path) -> None:
    spec, _spec_path, _spec_sha256 = _stored_spec(tmp_path)
    roles = {
        "1": ("INPUT", "input"),
        "2": ("OUTPUT", "output"),
        "3": ("GND", "passive"),
        "5": ("VDD", "power_in"),
    }
    typed_pins: list[PinSpec] = []
    for pin in spec.pins:
        role = roles.get(pin.number)
        typed_pins.append(
            pin.model_copy(update={"name": role[0], "electrical_type": role[1]})
            if role is not None
            else pin
        )
    typed_spec = spec.model_copy(update={"pins": typed_pins})
    spec_path = tmp_path / "typed.spec.json"
    spec_path.write_text(typed_spec.model_dump_json(indent=2), encoding="utf-8")
    loaded_spec = load_part_spec(spec_path)
    library = tmp_path / "typed.kicad_sym"
    libwriter.write_symbol(
        loaded_spec,
        library,
        spec_sha256=part_spec_sha256(spec_path),
        footprint_id="Fixture:VQFN",
    )
    pins = {pin.number: pin for pin in parse_symbol(library, loaded_spec.mpn).pins}
    assert pins["1"].x < 0
    assert pins["2"].x > 0
    assert pins["3"].y < 0
    assert pins["17"].y < 0
    assert pins["5"].y > 0


def test_writers_prefer_datasheet_url_when_available(tmp_path: Path) -> None:
    original, _spec_path, _sha256 = _stored_spec(tmp_path)
    url_spec = original.model_copy(
        update={
            "datasheet": original.datasheet.model_copy(
                update={"url": "https://example.test/datasheet.pdf"}
            )
        }
    )
    spec_path = tmp_path / "url.spec.json"
    spec_path.write_text(url_spec.model_dump_json(indent=2), encoding="utf-8")
    spec = load_part_spec(spec_path)
    spec_sha256 = part_spec_sha256(spec_path)
    footprint = tmp_path / "url.kicad_mod"
    libwriter.write_footprint(
        spec,
        compute_land_pattern(spec),
        footprint,
        spec_sha256=spec_sha256,
    )
    symbol = tmp_path / "url.kicad_sym"
    libwriter.write_symbol(
        spec,
        symbol,
        spec_sha256=spec_sha256,
        footprint_id="Fixture:VQFN",
    )
    assert parse_footprint(footprint).properties["Datasheet"] == spec.datasheet.url
    assert parse_symbol(symbol, spec.mpn).properties["Datasheet"] == spec.datasheet.url


def test_symbol_writer_preserves_other_symbols_in_sorted_order(tmp_path: Path) -> None:
    spec, _spec_path, spec_sha256 = _stored_spec(tmp_path)
    library = tmp_path / "Fixture.kicad_sym"
    libwriter.write_symbol(
        spec,
        library,
        spec_sha256=spec_sha256,
        symbol_name="B_SYMBOL",
        footprint_id="Fixture:VQFN",
    )
    libwriter.write_symbol(
        spec,
        library,
        spec_sha256=spec_sha256,
        symbol_name="A_SYMBOL",
        footprint_id="Fixture:VQFN",
    )

    root = sexpr.parse_text(library.read_text(encoding="utf-8"))
    names = [
        str(node[1])
        for node in root[1:]
        if isinstance(node, list) and len(node) >= 2 and node[0] == "symbol"
    ]
    assert names == ["A_SYMBOL", "B_SYMBOL"]
    assert len(parse_symbol(library, "A_SYMBOL").pins) == len(spec.pins)
    assert len(parse_symbol(library, "B_SYMBOL").pins) == len(spec.pins)


def test_generated_library_items_pass_targeted_library_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_spec, land, spec_path, _check_path, symbol_path, footprint_path = _write_case(
        tmp_path,
        monkeypatch,
        spec=_vqfn_spec(),
        record_authoring=False,
    )
    spec = load_part_spec(spec_path)
    spec_sha256 = part_spec_sha256(spec_path)
    model_path = next((tmp_path / "models").rglob("*.step"))
    libwriter.write_footprint(
        spec,
        land,
        footprint_path,
        spec_sha256=spec_sha256,
        model_path=str(model_path),
        ep_paste_margin_mm=-0.15,
    )
    libwriter.write_symbol(
        spec,
        symbol_path,
        spec_sha256=spec_sha256,
        footprint_id=f"Fixture:{spec.package.drawing_id}",
    )

    report = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        reference=land,
        library_dir=tmp_path / "library",
        model_required=False,
        test_board=False,
    )
    target_codes = {
        finding.code
        for finding in report.findings
        if finding.code.startswith(("symbol_", "pad_", "land_", "courtyard_"))
        or finding.code in {"fab_outline", "silk_over_pad"}
    }
    assert target_codes == set()
