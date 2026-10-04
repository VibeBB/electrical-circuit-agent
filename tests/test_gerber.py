import math
from pathlib import Path

import pytest

from circuit.gerber import ExportParseError, parse_excellon, parse_gerber


def test_parse_gerber_standard_apertures_and_regions(tmp_path: Path) -> None:
    source = tmp_path / "fixture.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%ADD10C,0.500*%\n"
        "%ADD11R,1.000X0.600*%\n"
        "D10*\n"
        "X0001000000Y0002000000D03*\n"
        "D11*\n"
        "X0003000000Y0004000000D03*\n"
        "G36*\n"
        "X0000000000Y0000000000D02*\n"
        "X0001000000Y0000000000D01*\n"
        "X0001000000Y0001000000D01*\n"
        "X0000000000Y0001000000D01*\n"
        "X0000000000Y0000000000D01*\n"
        "G37*\n"
        "M02*\n",
        encoding="ascii",
    )

    result = parse_gerber(source)

    assert result.unit == "mm"
    assert len(result.features) == 3
    assert (result.features[0].x, result.features[0].y) == pytest.approx((1.0, 2.0))
    assert result.features[0].area == pytest.approx(3.14159265 * 0.25**2)
    assert (result.features[1].width, result.features[1].height) == pytest.approx((1.0, 0.6))
    assert result.features[2].area == pytest.approx(1.0)


def test_parse_gerber_aperture_macro_for_roundrect_geometry(tmp_path: Path) -> None:
    source = tmp_path / "roundrect.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%AMRoundRect*21,1,1.0,0.5,0,0,0*%\n"
        "%ADD10RoundRect,1.0X0.5*%\n"
        "D10*\n"
        "X0001000000Y0002000000D03*\n"
        "M02*\n",
        encoding="ascii",
    )

    feature = parse_gerber(source).features[0]

    assert feature.shape == "macro"
    assert (feature.width, feature.height) == pytest.approx((1.0, 0.5))
    assert feature.area == pytest.approx(0.5, abs=0.005)


def test_parse_gerber_kicad_roundrect_macro_with_closed_outline(tmp_path: Path) -> None:
    source = tmp_path / "kicad-roundrect.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%AMRoundRect*\n"
        "4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*\n"
        "1,1,$1+$1,$2,$3*\n"
        "1,1,$1+$1,$4,$5*\n"
        "1,1,$1+$1,$6,$7*\n"
        "1,1,$1+$1,$8,$9*\n"
        "20,1,$1+$1,$2,$3,$4,$5,0*\n"
        "20,1,$1+$1,$4,$5,$6,$7,0*\n"
        "20,1,$1+$1,$6,$7,$8,$9,0*\n"
        "20,1,$1+$1,$8,$9,$2,$3,0*%\n"
        "%ADD10RoundRect,0.060000X-0.240000X-0.060000X0.240000X-0.060000X"
        "0.240000X0.060000X-0.240000X0.060000X0*%\n"
        "D10*\n"
        "X0000000000Y0000000000D03*\n"
        "M02*\n",
        encoding="ascii",
    )

    feature = parse_gerber(source).features[0]

    assert feature.shape == "macro"
    assert (feature.width, feature.height) == pytest.approx((0.6, 0.24))
    assert feature.area == pytest.approx(0.6 * 0.24 - (4 - math.pi) * 0.06**2, abs=0.001)


def test_parse_gerber_polygon_aperture_keeps_vertex_count_and_rotation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "polygon.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10P,1.000X5X30*%\nD10*\nX0000000000Y0000000000D03*\nM02*\n",
        encoding="ascii",
    )

    feature = parse_gerber(source).features[0]

    assert feature.shape == "P"
    expected_points = [
        (
            math.cos(math.radians(30 + 72 * index)) * 0.5,
            math.sin(math.radians(30 + 72 * index)) * 0.5,
        )
        for index in range(5)
    ]
    assert feature.polygon is not None
    assert len(feature.polygon) == 5
    for actual, expected in zip(feature.polygon, expected_points, strict=True):
        assert actual == pytest.approx(expected)
    assert feature.width == pytest.approx(
        max(point[0] for point in expected_points) - min(point[0] for point in expected_points)
    )
    assert feature.height == pytest.approx(
        max(point[1] for point in expected_points) - min(point[1] for point in expected_points)
    )
    assert feature.area == pytest.approx(0.5944103)


def test_parse_gerber_aperture_hole_reduces_copper_area(tmp_path: Path) -> None:
    source = tmp_path / "hole.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,1.000X0.500*%\nD10*\nX0000000000Y0000000000D03*\nM02*\n",
        encoding="ascii",
    )

    feature = parse_gerber(source).features[0]

    assert feature.area == pytest.approx(math.pi * (1.0**2 - 0.5**2) / 4)


def test_parse_gerber_accepts_empty_layer_file(tmp_path: Path) -> None:
    source = tmp_path / "empty.gbr"
    source.write_text("%FSLAX46Y46*%\n%MOMM*%\nM02*\n", encoding="ascii")

    assert parse_gerber(source).features == ()


def test_parse_gerber_d01_strokes_with_the_selected_aperture(tmp_path: Path) -> None:
    source = tmp_path / "stroke.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%ADD10C,0.500*%\n"
        "D10*\n"
        "X0000000000Y0000000000D02*\n"
        "X0001000000Y0000000000D01*\n"
        "M02*\n",
        encoding="ascii",
    )

    feature = parse_gerber(source).features[0]

    assert feature.shape == "stroke"
    assert (feature.width, feature.height) == pytest.approx((1.5, 0.5))


def test_parse_gerber_rejects_unsupported_interpolation(tmp_path: Path) -> None:
    source = tmp_path / "unsupported.gbr"
    source.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.500*%\nG02X0000100000Y0000200000D01*\n",
        encoding="ascii",
    )

    with pytest.raises(ExportParseError, match="unsupported Gerber"):
        parse_gerber(source)


def test_parse_excellon_tool_table_and_hits(tmp_path: Path) -> None:
    source = tmp_path / "holes.drl"
    source.write_text(
        "M48\n"
        ";FILE_FORMAT=2:4\n"
        "METRIC,LZ\n"
        "T01C0.600\n"
        "T02C0.800\n"
        "%\n"
        "T01\n"
        "X10000Y20000\n"
        "T02X30000Y40000\n"
        "M30\n",
        encoding="ascii",
    )

    result = parse_excellon(source)

    assert result.unit == "mm"
    assert [(hit.x, hit.y, hit.diameter) for hit in result.hits] == pytest.approx(
        [(1.0, 2.0, 0.6), (3.0, 4.0, 0.8)]
    )


def test_parse_excellon_rejects_unknown_routing(tmp_path: Path) -> None:
    source = tmp_path / "slot.drl"
    source.write_text(
        "M48\nMETRIC,TZ\nT01C0.500\n%\nT01\nG85X1000Y1000\nM30\n",
        encoding="ascii",
    )

    with pytest.raises(ExportParseError, match="unsupported Excellon"):
        parse_excellon(source)


def test_parse_excellon_accepts_empty_drill_file(tmp_path: Path) -> None:
    source = tmp_path / "empty.drl"
    source.write_text("M48\nMETRIC,TZ\n%\nM30\n", encoding="ascii")

    assert parse_excellon(source).hits == ()


def test_parse_excellon_applies_trailing_zero_suppression(tmp_path: Path) -> None:
    source = tmp_path / "trailing-zero.drl"
    source.write_text(
        "M48\n;FILE_FORMAT=2:4\nMETRIC,TZ\nT01C0.600\n%\nT01X01Y02\nM30\n",
        encoding="ascii",
    )

    result = parse_excellon(source)

    assert [(hit.x, hit.y) for hit in result.hits] == [(1.0, 2.0)]
