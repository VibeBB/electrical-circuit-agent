from pathlib import Path

import pytest

from circuit.kicad_cli import KicadCliError
from circuit.stackup import stackup_svg, write_stackup_diagram


def _stackup() -> dict[str, object]:
    return {
        "layers": [
            {
                "layer": "BL_F_SilkS",
                "userName": "F.Silkscreen",
                "type": "BSLT_SILKSCREEN",
                "enabled": True,
                "thickness": {},
            },
            {
                "layer": "BL_F_Cu",
                "userName": "F.Cu",
                "type": "BSLT_COPPER",
                "enabled": True,
                "thickness": {"valueNm": "35000"},
                "materialName": "copper",
            },
            {
                "layer": "BL_UNDEFINED",
                "type": "BSLT_DIELECTRIC",
                "enabled": True,
                "thickness": {"valueNm": "1530000"},
                "dielectric": {
                    "type": "BSDT_CORE",
                    "layer": [
                        {
                            "materialName": "FR4",
                            "epsilonR": 4.5,
                            "lossTangent": 0.02,
                            "thickness": {"valueNm": "1530000"},
                        }
                    ],
                },
            },
            {
                "layer": "BL_B_Cu",
                "userName": "B.Cu",
                "type": "BSLT_COPPER",
                "enabled": False,
                "thickness": {"valueNm": "35000"},
            },
        ]
    }


def test_stackup_svg_bands_and_labels() -> None:
    svg = stackup_svg(_stackup())
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    # disabled layer skipped: 3 enabled bands + white background rect
    assert svg.count("<rect") == 4
    assert "F.Cu" in svg and "copper" in svg and "0.035 mm" in svg
    assert "FR4" in svg and "er=4.5" in svg and "tan=0.02" in svg
    assert "1.530 mm" in svg
    assert "B.Cu" not in svg


def test_stackup_svg_zero_thickness_layer_stays_visible() -> None:
    svg = stackup_svg({"layers": [{"type": "BSLT_SILKSCREEN", "enabled": True}]})
    assert "<rect" in svg and "layer" in svg


def test_stackup_svg_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(KicadCliError, match="layers"):
        stackup_svg({})
    with pytest.raises(KicadCliError, match="enabled"):
        stackup_svg({"layers": [{"type": "BSLT_COPPER", "enabled": False}]})
    out = write_stackup_diagram(_stackup(), tmp_path / "sub" / "board-stackup.svg")
    assert out.read_text(encoding="utf-8").startswith("<svg")
