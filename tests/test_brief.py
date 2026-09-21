import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit.brief import DesignBrief, brief_sha256, expected_nets, load_brief

ROOT = Path(__file__).parent


def test_fixture_loads_and_expected_nets() -> None:
    path = ROOT / "data" / "brief_led_loop.json"
    brief = load_brief(path)
    assert brief.name == "led_loop"
    assert expected_nets(brief)["LED_A"] == frozenset({("R1", "2"), ("D1", "2")})
    assert len(brief_sha256(path)) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "bad name"),
        ("parts", []),
        ("nets", []),
    ],
)
def test_brief_field_constraints(field: str, value: object) -> None:
    value_dict = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value_dict[field] = value
    with pytest.raises(ValidationError):
        DesignBrief.model_validate(value_dict)


def test_brief_rejects_duplicate_reference() -> None:
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["parts"][1]["reference"] = "J1"
    with pytest.raises(ValidationError, match="unique"):
        DesignBrief.model_validate(value)


def test_brief_rejects_duplicate_net_and_reused_pin() -> None:
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["nets"][1]["name"] = "VIN"
    with pytest.raises(ValidationError):
        DesignBrief.model_validate(value)
    value["nets"][1]["name"] = "LED_A"
    value["nets"][1]["pins"][0] = "J1.1"
    with pytest.raises(ValidationError, match="multiple nets"):
        DesignBrief.model_validate(value)


def test_brief_rejects_unknown_pin_and_placement() -> None:
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["nets"][0]["pins"][0] = "U1.1"
    with pytest.raises(ValidationError, match="unknown"):
        DesignBrief.model_validate(value)
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["board"]["placements"]["U1"] = {"x_mm": 1, "y_mm": 1}
    with pytest.raises(ValidationError, match="unknown"):
        DesignBrief.model_validate(value)


def test_brief_rejects_outside_placement() -> None:
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["board"]["placements"]["J1"]["x_mm"] = 45
    with pytest.raises(ValidationError, match="outside"):
        DesignBrief.model_validate(value)


def test_brief_rejects_extra_fields_and_bad_library_ids() -> None:
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["parts"][0]["extra"] = True
    with pytest.raises(ValidationError):
        DesignBrief.model_validate(value)
    value = json.loads((ROOT / "data" / "brief_led_loop.json").read_text(encoding="utf-8"))
    value["parts"][0]["lib_id"] = "Connector_Generic"
    with pytest.raises(ValidationError, match="colon"):
        DesignBrief.model_validate(value)
