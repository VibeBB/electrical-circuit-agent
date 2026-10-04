from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit import sexpr
from circuit.libitems import (
    FootprintDef,
    LibItemError,
    parse_footprint,
    parse_symbol,
)

DATA = Path(__file__).parent / "data" / "library"


def test_sexpr_serialize_round_trip_with_quoted_atoms() -> None:
    expression: sexpr.SExpr = [
        "root",
        ["version", "20241209"],
        ["name", "part with spaces"],
        ["quote", 'a"b'],
        ["path", r"C:\folder\part"],
        ["line", "first\nsecond"],
        ["punctuation", "(nested)"],
    ]
    assert sexpr.parse_text(sexpr.serialize(expression)) == expression
    quoted_expression = sexpr.parse_text('(root (quoted "F.Cu") (bare F.Cu))')
    assert sexpr.serialize(quoted_expression) == '(root (quoted "F.Cu") (bare F.Cu))'


def test_parse_modern_footprint_fields_and_graphics() -> None:
    footprint = parse_footprint(DATA / "modern.kicad_mod")
    assert footprint.name == "Modern"
    assert footprint.attributes == ["smd"]
    assert footprint.properties == {
        "Reference": "U**",
        "Value": "Modern",
        "Footprint": "Fixture:Modern",
    }
    assert footprint.pads[0].model_dump(mode="python") == {
        "number": "1",
        "type": "smd",
        "shape": "roundrect",
        "x": -1.0,
        "y": -1.0,
        "rotation": 90.0,
        "width": 1.0,
        "height": 0.5,
        "drill": None,
        "layers": ["F.Cu", "F.Paste", "F.Mask"],
        "roundrect_ratio": None,
        "paste_margin": None,
        "mask_margin": None,
        "polygon": None,
    }
    assert footprint.pads[1].drill == 0.4
    assert footprint.pads[1].layers == ["*.Cu", "*.Mask"]
    assert [graphic.kind for graphic in footprint.graphics] == [
        "line",
        "rect",
        "circle",
        "arc",
        "poly",
    ]
    assert footprint.graphics[0].width == 0.12
    assert len(footprint.graphics[3].points) == 3
    assert footprint.models[0].path == "${KICAD10_3DMODEL_DIR}/Test.step"
    assert footprint.models[0].offset == (1.0, 2.0, 3.0)
    assert footprint.models[0].rotate == (0.0, 0.0, 90.0)


def test_parse_legacy_footprint_forms() -> None:
    footprint = parse_footprint(DATA / "legacy.kicad_mod")
    assert footprint.name == "Legacy"
    assert footprint.attributes == ["through_hole"]
    assert footprint.properties == {"Reference": "REF**", "Value": "Legacy"}
    assert footprint.pads[0].number == "1"
    assert footprint.pads[0].drill == 0.5
    assert footprint.graphics[0].kind == "line"
    assert footprint.graphics[0].width == 0.15


def test_parse_symbol_resolves_extends_and_properties() -> None:
    symbol = parse_symbol(DATA / "symbols.kicad_sym", "Derived")
    assert symbol.name == "Derived"
    assert symbol.properties == {
        "Reference": "U",
        "Footprint": "Fixture:Base",
        "Value": "Derived",
    }
    assert [(pin.number, pin.name) for pin in symbol.pins] == [
        ("1", "DATA"),
        ("2", "READY"),
    ]
    assert symbol.pins[0].electrical_type == "input"
    assert symbol.pins[0].x == -2.54
    assert symbol.pins[0].length == 2.54
    assert symbol.pins[1].orientation == 180.0
    assert symbol.pins[1].unit == 1


def test_library_item_parsers_fail_closed(tmp_path: Path) -> None:
    malformed = tmp_path / "broken.kicad_mod"
    malformed.write_text("(broken", encoding="utf-8")
    with pytest.raises(LibItemError, match="could not parse"):
        parse_footprint(malformed)
    with pytest.raises(LibItemError, match="symbol not found"):
        parse_symbol(DATA / "symbols.kicad_sym", "Missing")

    cycle = tmp_path / "cycle.kicad_sym"
    cycle.write_text(
        '(kicad_symbol_lib (symbol "A" (extends "B")) (symbol "B" (extends "A")))',
        encoding="utf-8",
    )
    with pytest.raises(LibItemError, match="cyclic symbol inheritance"):
        parse_symbol(cycle, "A")


def test_footprint_models_reject_unexpected_fields() -> None:
    value = parse_footprint(DATA / "modern.kicad_mod").model_dump(mode="python")
    value["unexpected"] = True
    with pytest.raises(ValidationError):
        FootprintDef.model_validate(value)
