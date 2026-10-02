import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from circuit import occt, sexpr
from circuit.libsource import LicenseInfo, SourceInfoInput, import_library_item
from circuit.libverify import cross_check_models
from circuit.model3d import GeneratedModel, Model3dError, footprint_to_board_xy, generate_model
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)

PackageFamily = Literal[
    "chip",
    "gullwing_dual",
    "gullwing_quad",
    "no_lead_dual",
    "no_lead_quad",
]


def _reading(text: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=text,
        vision_record="fixture",
    )


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _dimension_limits(minimum: float, maximum: float) -> Dimension:
    return Dimension(min=minimum, max=maximum, reading=_reading(f"{minimum}-{maximum}"))


def _fixture(
    family: PackageFamily,
) -> tuple[PartSpec, list[tuple[str, float, float, float, float]]]:
    pads: list[tuple[str, float, float, float, float]] = []
    exposed_pad: ExposedPad | None = None
    pins_per_side: tuple[int, int, int, int] | None = None
    lead_span: float | None = None
    pitch: float | None = None
    lead_length = 0.4
    lead_width = 0.3
    if family == "chip":
        body_length, body_width, pin_count = 2.0, 1.0, 2
        pads = [("1", -1.2, 0.0, 0.4, 0.4), ("2", 1.2, 0.0, 0.4, 0.4)]
    elif family == "gullwing_dual":
        body_length, body_width, pin_count = 4.0, 2.0, 4
        pitch, lead_span = 1.0, 5.0
        pads = [
            (str(index + 1), x, y, 0.3, 0.4)
            for index, (x, y) in enumerate(((-0.5, -2.3), (0.5, -2.3), (-0.5, 2.3), (0.5, 2.3)))
        ]
    elif family == "gullwing_quad":
        body_length, body_width, pin_count = 2.0, 2.0, 16
        pitch, lead_span, lead_width = 0.5, 3.2, 0.25
        positions = (-0.75, -0.25, 0.25, 0.75)
        pads = [
            (str(index + 1), x, y, 0.25, 0.5)
            for index, (x, y) in enumerate(
                [
                    *((x, -1.35) for x in positions),
                    *((1.35, y) for y in positions),
                    *((x, 1.35) for x in reversed(positions)),
                    *((-1.35, y) for y in reversed(positions)),
                ]
            )
        ]
        pins_per_side = (4, 4, 4, 4)
    elif family == "no_lead_dual":
        body_length, body_width, pin_count = 4.0, 2.0, 4
        pitch, lead_span = 1.0, 4.8
        pads = [
            (str(index + 1), x, y, 0.3, 0.4)
            for index, (x, y) in enumerate(((-0.5, -2.2), (0.5, -2.2), (-0.5, 2.2), (0.5, 2.2)))
        ]
    elif family == "no_lead_quad":
        body_length, body_width, pin_count = 3.0, 3.0, 16
        pitch, lead_span, lead_width = 0.5, 3.8, 0.25
        positions = (-0.75, -0.25, 0.25, 0.75)
        pads = [
            (str(index + 1), x, y, 0.25, 0.5)
            for index, (x, y) in enumerate(
                [
                    *((x, -1.65) for x in positions),
                    *((1.65, y) for y in positions),
                    *((x, 1.65) for x in reversed(positions)),
                    *((-1.65, y) for y in reversed(positions)),
                ]
            )
        ]
        pads.append(("17", 0.0, 0.0, 0.8, 0.8))
        exposed_pad = ExposedPad(
            number="17",
            width=_dimension(0.8),
            length=_dimension(0.8),
        )
        pins_per_side = (4, 4, 4, 4)
    else:
        raise AssertionError(family)

    package = PackageSpec(
        family=family,
        drawing_id=f"fixture-{family}",
        pin_count=pin_count,
        pitch=_dimension(pitch) if pitch is not None else None,
        body_length=_dimension(body_length),
        body_width=_dimension(body_width),
        height=_dimension(0.8),
        standoff=_dimension(0.2),
        lead_span=_dimension(lead_span) if lead_span is not None else None,
        lead_length=_dimension(lead_length),
        lead_width=_dimension(lead_width),
        exposed_pad=exposed_pad,
        pins_per_side=pins_per_side,
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin one is top left"),
    )
    pins = [
        PinSpec(
            number=number,
            name=f"PIN{number}",
            electrical_type="passive",
            reading=_reading(f"{number} PIN{number}"),
        )
        for number, *_ in pads
    ]
    spec = PartSpec(
        artifact_kind="circuit_part_spec",
        mpn=f"FIXTURE-{family}",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=package,
        pins=pins,
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn=f"FIXTURE-{family}",
                package_designator=family,
                pin_count=pin_count,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading(f"FIXTURE-{family} {family}"),
            )
        ],
    )
    return spec, pads


def _write_footprint(
    path: Path,
    pads: list[tuple[str, float, float, float, float]],
) -> None:
    nodes: list[sexpr.SExpr] = ["footprint", sexpr.quoted("fixture")]
    for number, x, y, width, height in pads:
        nodes.append(
            [
                "pad",
                sexpr.quoted(number),
                "smd",
                "rect",
                ["at", str(x), str(y)],
                ["size", str(width), str(height)],
                ["layers", sexpr.quoted("F.Cu"), sexpr.quoted("F.Mask")],
            ]
        )
    path.write_text(sexpr.serialize(nodes) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "family",
    ["chip", "gullwing_dual", "gullwing_quad", "no_lead_dual", "no_lead_quad"],
)
def test_generate_model_lands_terminals_and_is_deterministic(
    family: PackageFamily,
    tmp_path: Path,
) -> None:
    spec, pads = _fixture(family)
    footprint = tmp_path / f"{family}.kicad_mod"
    _write_footprint(footprint, pads)

    first = generate_model(spec, footprint, tmp_path / "out")
    first_bytes = first.step_path.read_bytes()
    second = generate_model(spec, footprint, tmp_path / "out")

    assert first_bytes == second.step_path.read_bytes()
    assert first.step_sha256 == hashlib.sha256(first_bytes).hexdigest()
    shape = occt.read_step(first.step_path)
    facts = occt.inspect(shape)
    assert facts.valid
    assert facts.units == "mm"
    assert facts.solid_count == len(pads) + 1
    assert max(solid.bbox.z_max for solid in facts.solids) == pytest.approx(spec.package.height.nom)
    regions = occt.slab_regions(shape, 0.0, 0.02)
    assert len(regions) == len(pads)
    expected_pads = sorted(
        pads, key=lambda pad: (not pad[0].isdigit(), int(pad[0]) if pad[0].isdigit() else pad[0])
    )
    if family == "no_lead_quad":
        expected_pads = [pad for pad in expected_pads if pad[0] != "17"] + [
            next(pad for pad in expected_pads if pad[0] == "17")
        ]
    actual_regions = sorted(regions, key=lambda region: region.source_solid)
    for region, (_, expected_x, expected_y, _, _) in zip(
        actual_regions, expected_pads, strict=True
    ):
        center_x = (region.bbox_xy[0] + region.bbox_xy[2]) / 2
        center_y = (region.bbox_xy[1] + region.bbox_xy[3]) / 2
        assert center_x == pytest.approx(expected_x, abs=1e-5)
        assert center_y == pytest.approx(expected_y, abs=1e-5)
    if family == "chip":
        assert first.marker is None
        assert first.marker_note is not None
    else:
        body = max(facts.solids, key=lambda solid: solid.volume)
        marker = occt.pin1_marker(shape, body.bbox)
        assert marker is not None
        assert marker.quadrant == "top_left"


def test_generate_model_derives_midpoint_nominals_and_records_them(tmp_path: Path) -> None:
    spec, pads = _fixture("chip")
    package = spec.package.model_copy(
        update={
            "body_length": _dimension_limits(1.8, 2.2),
            "body_width": _dimension_limits(0.9, 1.1),
            "height": _dimension_limits(0.7, 0.9),
        }
    )
    spec = spec.model_copy(update={"package": package})
    footprint = tmp_path / "chip.kicad_mod"
    _write_footprint(footprint, pads)

    generated = generate_model(spec, footprint, tmp_path / "out")
    manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))

    assert manifest["derived_nominals"] == {
        "body_length": "midpoint",
        "body_width": "midpoint",
        "height": "midpoint",
    }
    assert manifest["parameters"]["body_length_mm"] == pytest.approx(2.0)
    assert manifest["parameters"]["body_width_mm"] == pytest.approx(1.0)
    assert manifest["parameters"]["height_mm"] == pytest.approx(0.8)


def test_generate_model_rejects_dimension_with_only_maximum(tmp_path: Path) -> None:
    spec, pads = _fixture("chip")
    package = spec.package.model_copy(
        update={"body_length": Dimension(max=2.2, reading=_reading("maximum 2.2"))}
    )
    spec = spec.model_copy(update={"package": package})
    footprint = tmp_path / "chip.kicad_mod"
    _write_footprint(footprint, pads)

    with pytest.raises(
        Model3dError, match="body_length requires a nominal value or both min and max"
    ):
        generate_model(spec, footprint, tmp_path / "out")


def test_generate_model_clamps_small_standoff_for_slab_clearance(tmp_path: Path) -> None:
    spec, pads = _fixture("gullwing_dual")
    package = spec.package.model_copy(update={"standoff": _dimension(0.01)})
    spec = spec.model_copy(update={"package": package})
    footprint = tmp_path / "gullwing.kicad_mod"
    _write_footprint(footprint, pads)

    generated = generate_model(spec, footprint, tmp_path / "out")
    manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))

    assert manifest["parameters"]["body_bottom_mm"] == pytest.approx(0.03)
    assert len(occt.slab_regions(occt.read_step(generated.step_path), 0.0, 0.02)) == len(pads)


def test_generate_model_rejects_unsupported_family(tmp_path: Path) -> None:
    spec, pads = _fixture("chip")
    spec = spec.model_copy(update={"package": spec.package.model_copy(update={"family": "custom"})})
    footprint = tmp_path / "custom.kicad_mod"
    _write_footprint(footprint, pads)

    with pytest.raises(Model3dError, match=r"^unsupported_family$"):
        generate_model(spec, footprint, tmp_path / "out")


def test_kicad_cli_frame_transform_matches_asymmetric_fixture_observation() -> None:
    assert footprint_to_board_xy(0.5, -0.15, rotation_deg=0) == pytest.approx((0.5, -0.15))
    assert footprint_to_board_xy(0.5, -0.15, rotation_deg=90) == pytest.approx((0.15, 0.5))


def _import_step(path: Path, library_dir: Path, nickname: str) -> Path:
    report = import_library_item(
        path,
        library_dir,
        nickname,
        source=SourceInfoInput(
            origin="manufacturer",
            vendor="Fixture vendor",
            url="https://example.invalid/model",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            license=LicenseInfo(
                spdx="MIT",
                attribution="Fixture vendor",
                redistribution="allowed",
            ),
        ),
    )
    model = next(entry for entry in report.imported if entry.artifact == "model3d")
    return library_dir / model.path


def _generated_model(
    tmp_path: Path,
) -> tuple[PartSpec, Path, GeneratedModel]:
    spec, pads = _fixture("gullwing_dual")
    footprint = tmp_path / "Fixture.pretty" / "Fixture.kicad_mod"
    footprint.parent.mkdir(parents=True)
    _write_footprint(footprint, pads)
    generated = generate_model(spec, footprint, tmp_path / "generated")
    return spec, generated.step_path, generated


def test_cross_check_models_accepts_hash_bound_libsource_import(tmp_path: Path) -> None:
    spec, generated_path, generated = _generated_model(tmp_path)
    imported_path = _import_step(generated_path, tmp_path / "library", "Manufacturer")

    report = cross_check_models(generated, imported_path, spec)

    assert report.passed
    assert report.imported_provenance is not None
    assert report.imported_provenance.source.origin == "manufacturer"


def test_cross_check_models_rejects_terminal_center_disagreement(tmp_path: Path) -> None:
    spec, generated_path, _generated = _generated_model(tmp_path)
    shifted_path = tmp_path / "shifted.step"
    shifted = occt.transform(
        occt.read_step(generated_path),
        translation=(0.1, 0.0, 0.0),
    )
    occt.write_step(shifted, shifted_path, product_name="Shifted")
    imported_path = _import_step(shifted_path, tmp_path / "library", "Manufacturer")

    report = cross_check_models(generated_path, imported_path, spec)

    assert not report.passed
    assert any(
        finding.code == "model_source_disagreement" and "terminal center sets" in finding.message
        for finding in report.findings
    )


def test_cross_check_models_rejects_pin1_quadrant_disagreement(tmp_path: Path) -> None:
    spec, generated_path, generated = _generated_model(tmp_path)
    mirrored_path = tmp_path / "mirrored.step"
    mirrored = occt.transform(occt.read_step(generated_path), mirror_x=True)
    occt.write_step(mirrored, mirrored_path, product_name="Mirrored")
    imported_path = _import_step(mirrored_path, tmp_path / "library", "Manufacturer")

    report = cross_check_models(generated, imported_path, spec)

    assert not report.passed
    assert any("pin-1 quadrants" in finding.message for finding in report.findings)


def test_cross_check_models_rejects_body_extent_disagreement(tmp_path: Path) -> None:
    spec, generated_path, generated = _generated_model(tmp_path)
    scaled_path = tmp_path / "scaled.step"
    scaled = occt.transform(occt.read_step(generated_path), scale=1.1)
    occt.write_step(scaled, scaled_path, product_name="Scaled")
    imported_path = _import_step(scaled_path, tmp_path / "library", "Manufacturer")

    report = cross_check_models(generated, imported_path, spec)

    assert not report.passed
    assert any("body extents" in finding.message for finding in report.findings)


def test_cross_check_models_requires_valid_import_provenance(tmp_path: Path) -> None:
    spec, generated_path, generated = _generated_model(tmp_path)

    report = cross_check_models(generated, generated_path, spec)

    assert not report.passed
    assert report.imported_provenance is None
    assert any("hash-bound libsource provenance" in finding.message for finding in report.findings)


def test_cross_check_models_rejects_tampered_import_provenance(tmp_path: Path) -> None:
    spec, generated_path, generated = _generated_model(tmp_path)
    library_dir = tmp_path / "library"
    imported_path = _import_step(generated_path, library_dir, "Manufacturer")
    provenance_path = library_dir / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["entries"][0]["sha256"] = "0" * 64
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    report = cross_check_models(generated, imported_path, spec)

    assert not report.passed
    assert report.imported_provenance is None
