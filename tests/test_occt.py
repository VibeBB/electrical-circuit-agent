from pathlib import Path

import pytest

from circuit import occt


def test_step_round_trip_is_deterministic_and_inspectable(tmp_path: Path) -> None:
    shape = occt.compound(
        (
            occt.box(-1.0, -2.0, 0.0, 2.0, 4.0, 1.5),
            occt.box(3.0, 0.0, 0.0, 0.5, 0.5, 0.5),
        )
    )
    step_path = tmp_path / "model.step"

    occt.write_step(shape, step_path, product_name="fixture")
    first = step_path.read_bytes()
    occt.write_step(shape, step_path, product_name="fixture")

    assert step_path.read_bytes() == first
    assert b"1970-01-01T00:00:00" in first
    facts = occt.inspect(occt.read_step(step_path))
    assert facts.solid_count == 2
    assert facts.valid
    assert facts.units == "mm"
    assert facts.solids[0].volume == pytest.approx(12.0)
    assert facts.solids[0].closed_shell
    assert facts.solids[1].volume == pytest.approx(0.125)
    assert facts.solids[0].bbox.xyz == pytest.approx((-1.0, -2.0, 0.0, 1.0, 2.0, 1.5), abs=2e-7)


def test_slab_regions_preserve_source_solid_and_area() -> None:
    first = occt.box(-1.0, -1.5, 0.0, 2.0, 3.0, 0.2)
    second = occt.box(2.0, -1.0, 0.0, 2.0, 2.0, 0.2)

    regions = occt.slab_regions(occt.compound((first, second)), 0.0, 0.02)

    assert len(regions) == 2
    assert [region.source_solid for region in regions] == [0, 1]
    assert [region.area for region in regions] == pytest.approx([6.0, 4.0])


def test_pin1_marker_finds_recessed_top_quadrant() -> None:
    body = occt.box(-1.0, -1.0, 0.0, 2.0, 2.0, 1.0)
    marked = occt.cylinder_cut(body, x=-0.5, y=-0.5, top_z=1.0, radius=0.2, depth=0.05)
    bounds = occt.Bounds(-1.0, -1.0, 0.0, 1.0, 1.0, 1.0)

    marker = occt.pin1_marker(marked, bounds)

    assert marker is not None
    assert marker.quadrant == "top_left"


def test_invalid_step_is_rejected_and_color_read_is_best_effort(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.step"
    invalid_path.write_text("not a STEP file\n", encoding="utf-8")

    with pytest.raises(occt.OcctError):
        occt.read_step(invalid_path)
    assert occt.face_color_marker(invalid_path, occt.Bounds(-1.0, -1.0, 0.0, 1.0, 1.0, 1.0)) is None
