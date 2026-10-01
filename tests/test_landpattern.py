import hashlib
import json
import math
from pathlib import Path
from typing import Literal

import pytest

from circuit.landpattern import (
    LandPatternError,
    compute_land_pattern,
    lead_rects,
)
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    LandPad,
    LandPattern,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)
from circuit.ruleprofile import (
    EvidenceRef,
    GoalOverride,
    PasteRule,
    RuleProfile,
    load_rules,
    profile_sha256,
)
from pinout_fixtures import pinout_drawing

TestFamily = Literal[
    "no_lead_quad",
    "no_lead_dual",
    "gullwing_quad",
    "gullwing_dual",
    "chip",
    "through_hole_inline",
]


def _reading(vision: str = "package drawing") -> Reading:
    return Reading(
        page=1,
        bbox=(0, 0, 1, 1),
        vision=vision,
        vision_record="vision.json",
    )


def _dimension(
    nominal: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> Dimension:
    values = [value for value in (minimum, nominal, maximum) if value is not None]
    vision = " ".join(str(value) for value in values)
    return Dimension(
        min=minimum,
        nom=nominal,
        max=maximum,
        reading=_reading(vision),
    )


def _spec(
    *,
    family: TestFamily,
    pin_count: int,
    body_length: Dimension,
    body_width: Dimension,
    pitch: Dimension | None = None,
    lead_span: Dimension | None = None,
    lead_length: Dimension | None = None,
    lead_width: Dimension | None = None,
    pins_per_side: tuple[int, int, int, int] | None = None,
    exposed_pad: ExposedPad | None = None,
    land_pattern: LandPattern | None = None,
) -> PartSpec:
    package = PackageSpec(
        family=family,
        drawing_id="TEST",
        pin_count=pin_count,
        pitch=pitch,
        body_length=body_length,
        body_width=body_width,
        height=_dimension(1.0),
        lead_span=lead_span,
        lead_length=lead_length,
        lead_width=lead_width,
        exposed_pad=exposed_pad,
        pins_per_side=pins_per_side,
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("Pin 1 top-left"),
    )
    pin_numbers = [str(number) for number in range(1, pin_count + 1)]
    if exposed_pad is not None:
        pin_numbers.append(exposed_pad.number)
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST-1",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="parts.pdf",
            sha256=hashlib.sha256(b"pdf").hexdigest(),
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pinout=(
            pinout_drawing({str(number): f"PIN{number}" for number in range(1, pin_count + 1)})
            if family in {"no_lead_quad", "no_lead_dual", "gullwing_quad", "gullwing_dual"}
            else None
        ),
        land_pattern=land_pattern,
        pins=[
            PinSpec(
                number=number,
                name=f"PIN{number}",
                electrical_type="passive",
                reading=_reading(f"{number} PIN{number}"),
            )
            for number in pin_numbers
        ],
        orderable=[
            OrderableVariant(
                mpn="TEST-1",
                package_designator="TEST",
                pin_count=pin_count,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TEST-1 TEST"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def test_gullwing_nominal_geometry_and_konnect_pad_schema() -> None:
    spec = _spec(
        family="gullwing_dual",
        pin_count=8,
        body_length=_dimension(4.9),
        body_width=_dimension(3.9),
        pitch=_dimension(1.27),
        lead_span=_dimension(6.0),
        lead_length=_dimension(1.0),
        lead_width=_dimension(0.5),
    )

    result = compute_land_pattern(spec)

    assert result.source == "ipc7351b"
    assert result.params["toe"] == 0.35
    assert result.params["heel"] == 0.35
    assert result.params["side"] == 0.03
    assert result.params["Zmax"] == pytest.approx(6.7559016994)
    assert result.params["Gmin"] == pytest.approx(3.2440983006)
    assert result.params["Xmax"] == pytest.approx(0.6159016994)
    assert [(pad.number, pad.x, pad.y, pad.width, pad.height) for pad in result.pads] == [
        ("1", -2.5, -1.91, 1.76, 0.62),
        ("2", -2.5, -0.64, 1.76, 0.62),
        ("3", -2.5, 0.64, 1.76, 0.62),
        ("4", -2.5, 1.91, 1.76, 0.62),
        ("5", 2.5, 1.91, 1.76, 0.62),
        ("6", 2.5, 0.64, 1.76, 0.62),
        ("7", 2.5, -0.64, 1.76, 0.62),
        ("8", 2.5, -1.91, 1.76, 0.62),
    ]
    required = {"number", "type", "shape", "x", "y", "width", "height"}
    assert required <= result.konnect_pads[0].keys()
    assert result.konnect_pads[0]["type"] == "smd"
    assert result.konnect_pads[0]["rotation"] == 0.0
    assert result.courtyard == (-3.63, -2.7, 3.63, 2.7)


def test_no_lead_geometry_exposed_pad_and_lead_rectangles() -> None:
    spec = _spec(
        family="no_lead_quad",
        pin_count=16,
        body_length=_dimension(3.0),
        body_width=_dimension(3.0),
        pitch=_dimension(0.5),
        lead_length=_dimension(0.25),
        lead_width=_dimension(0.24),
        pins_per_side=(4, 4, 4, 4),
        exposed_pad=ExposedPad(
            number="17",
            length=_dimension(1.68),
            width=_dimension(1.68),
        ),
    )

    result = compute_land_pattern(spec)
    leads = lead_rects(spec)

    assert result.params["toe"] == 0.3
    assert result.params["heel"] == 0.0
    assert result.params["side"] == -0.04
    assert result.params["left.Zmax"] == pytest.approx(3.6559016994)
    assert result.params["left.Gmin"] == pytest.approx(2.4440983006)
    assert result.params["left.Xmax"] == pytest.approx(0.2159016994)
    assert [(pad.number, pad.x, pad.y) for pad in result.pads[:4]] == [
        ("1", -1.53, -0.75),
        ("2", -1.53, -0.25),
        ("3", -1.53, 0.25),
        ("4", -1.53, 0.75),
    ]
    assert result.pads[-1] == LandPad(
        number="17", x=0.0, y=0.0, width=1.68, height=1.68, shape="roundrect"
    )
    assert result.konnect_pads[12]["rotation"] == 90.0
    assert result.konnect_pads[12]["width"] == 0.61
    assert result.konnect_pads[12]["height"] == 0.22
    assert leads["1"][0].x0 == pytest.approx(-1.5)
    assert leads["1"][0].y0 == pytest.approx(-0.87)
    assert leads["1"][0].x1 == pytest.approx(-1.25)
    assert leads["1"][0].y1 == pytest.approx(-0.63)


def test_chip_nominal_geometry_and_goal_selection() -> None:
    spec = _spec(
        family="chip",
        pin_count=2,
        body_length=_dimension(1.6),
        body_width=_dimension(0.8),
        lead_length=_dimension(0.1),
    )

    result = compute_land_pattern(spec)

    assert result.params["toe"] == 0.2
    assert result.params["side"] == 0.0
    assert result.params["courtyard_excess"] == 0.15
    assert result.params["Zmax"] == pytest.approx(2.0559016994)
    assert result.params["Gmin"] == pytest.approx(1.3440983006)
    assert result.params["Xmax"] == pytest.approx(0.8559016994)
    assert [(pad.number, pad.x, pad.y, pad.width, pad.height) for pad in result.pads] == [
        ("1", -0.85, 0.0, 0.36, 0.86),
        ("2", 0.85, 0.0, 0.36, 0.86),
    ]
    assert lead_rects(spec)["1"][0].x0 == pytest.approx(-0.9)


def test_datasheet_pads_pass_through_without_modification() -> None:
    datasheet_pad = LandPad(
        number="1",
        x=-0.4,
        y=0.0,
        width=0.7,
        height=0.3,
        shape="rect",
    )
    spec = _spec(
        family="chip",
        pin_count=2,
        body_length=_dimension(1.0),
        body_width=_dimension(0.5),
        lead_length=_dimension(0.1),
        land_pattern=LandPattern(
            source="datasheet",
            dimensions={"pad_pitch": _dimension(1.0)},
            pads=[datasheet_pad],
        ),
    )

    result = compute_land_pattern(spec)

    assert result.source == "datasheet"
    assert result.pads == [datasheet_pad]
    assert result.konnect_pads[0]["shape"] == "rect"
    assert result.konnect_pads[0]["width"] == 0.7
    assert result.konnect_pads[0]["height"] == 0.3


def test_density_overrides_and_decimal_rounding() -> None:
    spec = _spec(
        family="gullwing_dual",
        pin_count=4,
        body_length=_dimension(3.0),
        body_width=_dimension(2.0),
        pitch=_dimension(0.625),
        lead_span=_dimension(4.0),
        lead_length=_dimension(0.5),
        lead_width=_dimension(0.4),
    )

    least = compute_land_pattern(spec, "least", fabrication_tolerance=0.1)
    most = compute_land_pattern(spec, "most", placement_tolerance=0.05)

    assert least.params["side"] == -0.04
    assert least.params["toe"] == 0.15
    assert least.params["F"] == 0.1
    assert most.params["side"] == 0.01
    assert most.params["toe"] == 0.55
    assert most.params["P"] == 0.05

    rounding_spec = _spec(
        family="gullwing_dual",
        pin_count=4,
        body_length=_dimension(4.901),
        body_width=_dimension(2.0),
        pitch=_dimension(1.27),
        lead_span=_dimension(6.0),
        lead_length=_dimension(1.0),
        lead_width=_dimension(0.8890983005625053),
    )
    rounded = compute_land_pattern(rounding_spec)
    assert rounded.pads[0].height == 1.01
    assert rounded.courtyard[3] == 2.71


def test_maximum_material_lead_rects_and_asymmetric_quad_pin_order() -> None:
    spec = _spec(
        family="no_lead_quad",
        pin_count=8,
        body_length=_dimension(None, 2.8, 3.0),
        body_width=_dimension(None, 3.8, 4.0),
        pitch=_dimension(0.5),
        lead_length=_dimension(None, 0.2, 0.3),
        lead_width=_dimension(None, 0.2, 0.24),
        pins_per_side=(3, 1, 3, 1),
    )

    result = compute_land_pattern(spec)
    leads = lead_rects(spec)

    assert [pad.number for pad in result.pads] == [str(i) for i in range(1, 9)]
    assert result.pads[0].y == -0.5
    assert result.pads[1].y == 0.0
    assert result.pads[2].y == 0.5
    assert result.pads[3].x == 0.0
    assert leads["1"][0].x0 == pytest.approx(-2.0)
    assert leads["1"][0].y0 == pytest.approx(-0.62)


@pytest.mark.parametrize(
    ("family", "pin_count", "lead_span", "lead_length", "lead_width", "pitch"),
    [
        ("through_hole_inline", 2, None, 0.2, 0.3, 1.0),
        ("gullwing_dual", 3, 4.0, 0.2, 0.3, 1.0),
        ("gullwing_dual", 4, 4.0, None, 0.3, 1.0),
    ],
)
def test_unsupported_or_incomplete_package_is_rejected(
    family: TestFamily,
    pin_count: int,
    lead_span: float | None,
    lead_length: float | None,
    lead_width: float | None,
    pitch: float | None,
) -> None:
    spec = _spec(
        family=family,
        pin_count=pin_count,
        body_length=_dimension(3.0),
        body_width=_dimension(2.0),
        pitch=_dimension(pitch) if pitch is not None else None,
        lead_span=_dimension(lead_span) if lead_span is not None else None,
        lead_length=_dimension(lead_length) if lead_length is not None else None,
        lead_width=_dimension(lead_width) if lead_width is not None else None,
    )

    with pytest.raises(LandPatternError):
        compute_land_pattern(spec)


def test_invalid_tolerance_and_quad_side_counts_are_rejected() -> None:
    spec = _spec(
        family="no_lead_quad",
        pin_count=16,
        body_length=_dimension(3.0),
        body_width=_dimension(3.0),
        pitch=_dimension(0.5),
        lead_length=_dimension(0.25),
        lead_width=_dimension(0.24),
        pins_per_side=(4, 4, 4, 3),
    )

    with pytest.raises(LandPatternError, match="summing to pin_count"):
        compute_land_pattern(spec)
    with pytest.raises(LandPatternError, match="tolerances"):
        compute_land_pattern(spec, fabrication_tolerance=math.nan)


def test_effective_rules_and_explicit_overrides_preserve_default_behavior(
    tmp_path: Path,
) -> None:
    spec = _spec(
        family="gullwing_dual",
        pin_count=4,
        body_length=_dimension(3.0),
        body_width=_dimension(2.0),
        pitch=_dimension(1.27),
        lead_span=_dimension(4.0),
        lead_length=_dimension(0.5),
        lead_width=_dimension(0.4),
    )
    default = compute_land_pattern(spec)
    builtin = load_rules("builtin:ipc7351b", tmp_path / "library" / "rules")
    selected = compute_land_pattern(spec, rules=builtin)

    assert selected.params == default.params
    assert selected.pads == default.pads
    assert selected.courtyard == default.courtyard
    assert selected.rule_chain == ["builtin:ipc7351b"]
    assert selected.rule_chain_sha256 == builtin.chain_sha256

    rules_dir = tmp_path / "library" / "rules"
    child = RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="product.custom",
        layer="product",
        parent="builtin:ipc7351b",
        parent_sha256=profile_sha256(
            RuleProfile(
                artifact_kind="circuit_rule_profile",
                profile_id="builtin:ipc7351b",
                layer="standard",
                parent=None,
                standard="ipc7351b",
                density="nominal",
                fabrication_tolerance=0.05,
                placement_tolerance=0.025,
                min_pad_clearance_mm=0.15,
                min_ep_to_pad_clearance_mm=0.2,
                min_mask_web_mm=0.1,
                paste=PasteRule(
                    coverage_min=0.5,
                    coverage_max=1.0,
                    ep_coverage_min=0.5,
                    ep_coverage_max=0.8,
                ),
            )
        ),
        rationale="prototype measured",
        evidence=[
            EvidenceRef(
                kind="prototype",
                path="prototype.txt",
                sha256=hashlib.sha256(b"prototype").hexdigest(),
            )
        ],
        goal_overrides={"gullwing_dual": GoalOverride(toe=0.45)},
        fabrication_tolerance=0.08,
        placement_tolerance=0.04,
    )
    rules_dir.mkdir(parents=True)
    (tmp_path / "prototype.txt").write_text("prototype", encoding="utf-8")
    (rules_dir / "product.custom.json").write_text(
        json.dumps(child.model_dump(mode="json")), encoding="utf-8"
    )
    product = load_rules("product.custom", rules_dir)

    overridden = compute_land_pattern(
        spec,
        "most",
        rules=product,
        fabrication_tolerance=0.09,
        placement_tolerance=0.03,
    )
    assert overridden.params["F"] == 0.09
    assert overridden.params["P"] == 0.03
    assert overridden.params["toe"] == 0.45
    assert overridden.rule_chain == ["builtin:ipc7351b", "product.custom"]
    assert overridden.rule_chain_sha256 == product.chain_sha256

    from_profile = compute_land_pattern(spec, rules=product)
    assert from_profile.params["F"] == 0.08
    assert from_profile.params["P"] == 0.04
    assert from_profile.params["toe"] == 0.45
