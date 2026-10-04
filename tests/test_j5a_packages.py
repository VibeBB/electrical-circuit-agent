from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from circuit import libwriter, model3d, mutation, occt
from circuit.landpattern import compute_land_pattern
from circuit.libitems import parse_footprint, parse_symbol
from circuit.partspec import (
    BallGrid,
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    LandPad,
    LandPattern,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PartSpecReport,
    PinSpec,
    PinTable,
    Reading,
    TabSpec,
    part_spec_sha256,
)
from circuit.pinout import PinoutGeometry
from pinout_fixtures import QUAD16_NAMES, geometry_for_names
from test_libverify import (
    _vqfn_spec,  # pyright: ignore[reportPrivateUsage]
    _write_case,  # pyright: ignore[reportPrivateUsage]
    _write_comparison_records,  # pyright: ignore[reportPrivateUsage]
)


def _reading(value: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=value,
        vision_record="fixture.json",
    )


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _pin(number: str, name: str, *, bank: str | None = None) -> PinSpec:
    return PinSpec(
        number=number,
        name=name,
        electrical_type="passive",
        bank=bank,
        reading=_reading(f"{number} {name}"),
    )


def _base_spec(
    family: str,
    *,
    pin_count: int,
    drawing_id: str,
    pins: list[PinSpec],
    package_updates: dict[str, object] | None = None,
) -> PartSpec:
    package_data: dict[str, object] = {
        "family": family,
        "drawing_id": drawing_id,
        "pin_count": pin_count,
        "body_length": _dimension(3.0),
        "body_width": _dimension(2.0),
        "height": _dimension(1.0),
        "drawing_view": "top",
        "pin1_corner": "top_left",
        "pin1_reading": _reading("pin one top left"),
    }
    if package_updates is not None:
        package_data.update(package_updates)
    package = PackageSpec.model_validate(package_data)
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn=f"J5A-{drawing_id}",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="0" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=package,
        pins=pins,
        orderable=[
            OrderableVariant(
                mpn=f"J5A-{drawing_id}",
                package_designator=drawing_id,
                pin_count=pin_count,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading(f"J5A-{drawing_id} {drawing_id}"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _sot223_spec(*, depopulated: bool = False) -> PartSpec:
    numbers = ["1", "3"] if depopulated else ["1", "2", "3"]
    package_updates: dict[str, object] = {
        "pitch": _dimension(1.5),
        "body_length": _dimension(6.5),
        "body_width": _dimension(5.0),
        "height": _dimension(1.8),
        "lead_span": _dimension(7.2),
        "lead_length": _dimension(0.6),
        "lead_width": _dimension(0.6),
        "tab": TabSpec(
            number="4",
            width=_dimension(3.0),
            length=_dimension(1.8),
            offset=_dimension(4.1),
        ),
        "missing_pins": ["2"] if depopulated else [],
    }
    return _base_spec(
        "tabbed_dpak" if depopulated else "sot223",
        pin_count=len(numbers),
        drawing_id="TO252" if depopulated else "SOT223",
        pins=[*[_pin(number, f"PIN{number}") for number in numbers], _pin("4", "TAB")],
        package_updates=package_updates,
    )


def _sod_spec(*, banked: bool = False) -> PartSpec:
    package_updates: dict[str, object] = {
        "body_length": _dimension(3.5),
        "body_width": _dimension(1.7),
        "height": _dimension(1.0),
        "lead_span": _dimension(4.2),
        "lead_length": _dimension(1.15),
        "lead_width": _dimension(0.58),
    }
    pins = [
        _pin("1", "ANODE", bank="A" if banked else None),
        _pin("2", "CATHODE", bank="B" if banked else None),
    ]
    return _base_spec(
        "sod",
        pin_count=2,
        drawing_id="SOD123",
        pins=pins,
        package_updates=package_updates,
    )


def _bga_spec() -> PartSpec:
    rows = ["A", "B", "C"]
    columns = 2
    missing = ["B2"]
    numbers = [
        f"{row}{column}"
        for row in rows
        for column in range(1, columns + 1)
        if f"{row}{column}" not in missing
    ]
    package_updates: dict[str, object] = {
        "body_length": _dimension(2.5),
        "body_width": _dimension(1.5),
        "height": _dimension(1.0),
        "missing_pins": missing,
        "ball_grid": BallGrid(
            rows=rows,
            columns=columns,
            pitch_x=_dimension(1.0),
            pitch_y=_dimension(1.0),
            ball_diameter=_dimension(0.35),
        ),
    }
    return _base_spec(
        "bga",
        pin_count=len(numbers),
        drawing_id="DSBGA5",
        pins=[_pin(number, f"BALL{number}") for number in numbers],
        package_updates=package_updates,
    )


def _dqn_spec(*, dual_ep: bool = False) -> PartSpec:
    base = _vqfn_spec()
    base = base.model_copy(
        update={"package": base.package.model_copy(update={"lead_width": _dimension(0.24)})}
    )
    if dual_ep:
        package = base.package.model_copy(
            update={
                "exposed_pad": ExposedPad(
                    number="17",
                    length=_dimension(1.3),
                    width=_dimension(0.6),
                    center_x=_dimension(-0.55),
                ),
                "exposed_pads": [
                    ExposedPad(
                        number="18",
                        length=_dimension(1.3),
                        width=_dimension(0.6),
                        center_x=_dimension(0.55),
                    )
                ],
            }
        )
        assert base.land_pattern is not None
        pads = [
            LandPad(
                number=pad.number,
                x=-0.55 if pad.number == "17" else 0.55 if pad.number == "18" else pad.x,
                y=pad.y,
                width=0.6 if pad.number in {"17", "18"} else pad.width,
                height=1.3 if pad.number in {"17", "18"} else pad.height,
                shape=pad.shape,
                rotation=pad.rotation,
                polygon=pad.polygon,
                kind="exposed" if pad.number in {"17", "18"} else pad.kind,
            )
            for pad in base.land_pattern.pads
        ]
        pads.append(
            LandPad(
                number="18",
                x=0.55,
                y=0.0,
                width=0.6,
                height=1.3,
                shape="rect",
                kind="exposed",
            )
        )
        land_pattern = LandPattern(
            source="datasheet",
            dimensions=base.land_pattern.dimensions,
            pads=pads,
        )
        return base.model_copy(
            update={
                "package": package,
                "pins": [*base.pins, _pin("18", "EP")],
                "land_pattern": land_pattern,
            }
        )

    base_ep = base.package.exposed_pad
    assert base_ep is not None
    square_side = 1.68 / math.sqrt(2)
    half_side = square_side / 2
    exposed = base_ep.model_copy(
        update={
            "rotation_deg": 45.0,
            "polygon": [
                (-half_side, -half_side),
                (half_side, -half_side),
                (half_side, half_side),
                (-half_side, half_side),
            ],
        }
    )
    package = base.package.model_copy(update={"exposed_pad": exposed})
    assert base.land_pattern is not None
    signal_pads = [pad for pad in base.land_pattern.pads if pad.number != base_ep.number]
    polygon_pads = [
        LandPad(
            number=pad.number,
            x=pad.x,
            y=pad.y,
            width=pad.width,
            height=pad.height,
            shape="polygon",
            polygon=[
                (-pad.width / 2, -pad.height / 2),
                (pad.width / 2, -pad.height / 2),
                (pad.width / 2, pad.height / 2),
                (-pad.width / 2 + 0.08, pad.height / 2),
                (-pad.width / 2, pad.height / 2 - 0.08),
            ],
        )
        for pad in signal_pads
    ]
    polygon_pads.append(
        LandPad(
            number=base_ep.number,
            x=0.0,
            y=0.0,
            width=1.68,
            height=1.68,
            shape="polygon",
            rotation=45.0,
            polygon=exposed.polygon,
            kind="exposed",
        )
    )
    return base.model_copy(
        update={
            "package": package,
            "land_pattern": LandPattern(
                source="datasheet",
                dimensions=base.land_pattern.dimensions,
                pads=polygon_pads,
            ),
        }
    )


def _make_fixture(
    tmp_path: Path,
    spec: PartSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[mutation.MutationFixture, PartSpec, Path, Path, Path, Path]:
    spec, _reference, spec_path, _initial_check, library_path, footprint_path = _write_case(
        tmp_path,
        monkeypatch,
        spec=spec,
        footprint_kwargs={"model": None},
        record_comparisons=False,
    )
    monkeypatch.delenv("CIRCUIT_AUTHORING_LANE", raising=False)
    spec.bind_source_file(spec_path)
    land = compute_land_pattern(spec)
    footprint_sha = part_spec_sha256(spec_path)
    libwriter.write_symbol(
        spec,
        library_path,
        spec_sha256=footprint_sha,
        footprint_id=f"Fixture:{spec.package.drawing_id}",
        units="bank" if any(pin.bank is not None for pin in spec.pins) else "single",
    )
    model_output = tmp_path / "models"
    model_output.mkdir(parents=True, exist_ok=True)
    model_path = model_output / f"{spec.mpn}.3dshapes" / f"{spec.package.drawing_id}.step"
    libwriter.write_footprint(
        spec,
        land,
        footprint_path,
        spec_sha256=footprint_sha,
        model_path=str(model_path),
    )
    generated_model = model3d.generate_model(spec, footprint_path, model_output)
    assert generated_model.step_path == model_path
    _write_comparison_records(spec, spec_path, library_path, footprint_path)
    check_path = tmp_path / "part.spec.check.json"
    pinout: PinoutGeometry | None = None
    if spec.package.family == "no_lead_quad":
        pinout = geometry_for_names(QUAD16_NAMES, pin_count=16, topology="quad")
    check_report = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="pass",
        part_spec_sha256=footprint_sha,
        extraction_sha256="0" * 64,
        pdf_sha256=spec.datasheet.sha256,
        checked_readings=0,
        findings=[],
        pinout=pinout,
    )
    check_path.write_text(check_report.model_dump_json(indent=2), encoding="utf-8")
    fixture = mutation.library_mutation_fixture(
        spec_path=spec_path,
        spec_check_path=check_path,
        symbol_lib=library_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        model_path=model_path,
        work_dir=tmp_path / "mutation-work",
        run_export_oracle=False,
    )
    return fixture, spec, spec_path, library_path, footprint_path, model_path


def _single_operator_matrix(
    fixture: mutation.MutationFixture,
    operator_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> mutation.MutationReport:
    operator = next(item for item in mutation.MUTATION_OPERATORS if item.name == operator_name)
    assert operator.applies(fixture.artifacts)
    with monkeypatch.context() as patch:
        patch.setattr(mutation, "MUTATION_OPERATORS", (operator,))
        return mutation.run_mutations(fixture)


@pytest.mark.parametrize(
    ("spec_factory", "mutation_name"),
    [
        (_sot223_spec, "tab_offset_shift"),
        (lambda: _sot223_spec(depopulated=True), "depopulated_pin_added"),
        (_sod_spec, "pad_rotation_change"),
        (_bga_spec, "bga_row_swap"),
        (_dqn_spec, "polygon_vertex_shift"),
        (lambda: _dqn_spec(dual_ep=True), "exposed_pad_drop"),
    ],
)
def test_special_package_mutations_have_two_counting_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec_factory: Callable[[], PartSpec],
    mutation_name: str,
) -> None:
    fixture, _spec, _spec_path, _library, _footprint, _model = _make_fixture(
        tmp_path,
        spec_factory(),
        monkeypatch,
    )
    report = _single_operator_matrix(fixture, mutation_name, monkeypatch)
    assert report.passed is True
    assert report.single_oracle == []
    assert report.undetected == []
    assert len(report.outcomes) == 1
    assert report.outcomes[0].counting_family_count >= 2


@pytest.mark.parametrize(
    ("spec_factory", "expected_family"),
    [
        (_sot223_spec, "sot223"),
        (lambda: _sot223_spec(depopulated=True), "tabbed_dpak"),
        (_sod_spec, "sod"),
        (_bga_spec, "bga"),
        (_dqn_spec, "no_lead_quad"),
        (lambda: _dqn_spec(dual_ep=True), "no_lead_quad"),
    ],
)
def test_special_package_writer_and_verifier_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec_factory: Callable[[], PartSpec],
    expected_family: str,
) -> None:
    fixture, spec, _spec_path, _library, footprint_path, _model = _make_fixture(
        tmp_path,
        spec_factory(),
        monkeypatch,
    )
    report = fixture.verify(fixture.artifacts)
    findings = list(report)
    assert not [
        item.code
        for item in findings
        if item.code.startswith(("symbol_", "pad_", "land_", "fab_outline", "courtyard_", "silk_"))
    ]
    assert spec.package.family == expected_family
    parsed = parse_footprint(footprint_path)
    assert parsed.name == spec.package.drawing_id
    assert parsed.pads
    if (
        spec.package.family == "no_lead_quad"
        and spec.package.exposed_pad is not None
        and spec.package.exposed_pad.polygon is not None
    ):
        ep_pad = next(pad for pad in parsed.pads if pad.number == spec.package.exposed_pad.number)
        assert ep_pad.shape == "custom"
        assert ep_pad.rotation == 45.0
        assert ep_pad.polygon is not None
        assert len(ep_pad.polygon) == 4
    assert fixture.artifacts.model_path.is_file()
    assert occt.inspect(fixture.artifacts.model).valid


def test_banked_symbol_writer_preserves_pin_sets_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _sod_spec(banked=True)
    fixture, _spec, _spec_path, library_path, _footprint, _model = _make_fixture(
        tmp_path,
        spec,
        monkeypatch,
    )
    symbol = parse_symbol(library_path, fixture.artifacts.symbol.name)
    assert {pin.unit for pin in symbol.pins} == {1, 2}
    report = fixture.verify(fixture.artifacts)
    assert "symbol_bank_pin_set" not in {item.code for item in report}


def test_datasheet_land_pattern_keeps_polygon_rotation_and_pad_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _dqn_spec()
    _fixture, spec, _spec_path, _library, footprint_path, _model = _make_fixture(
        tmp_path,
        spec,
        monkeypatch,
    )
    land = compute_land_pattern(spec)
    assert land.source == "datasheet"
    assert spec.land_pattern is not None
    assert land.pads == spec.land_pattern.pads
    assert all(pad.shape == "polygon" for pad in land.pads)
    assert next(pad for pad in land.pads if pad.kind == "exposed").rotation == 45.0
    parsed = parse_footprint(footprint_path)
    assert any(pad.shape == "custom" for pad in parsed.pads)


def test_paste_margin_applies_to_exposed_pads_but_not_tabs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture, spec, spec_path, _library, _footprint, _model = _make_fixture(
        tmp_path / "exposed",
        _dqn_spec(dual_ep=True),
        monkeypatch,
    )
    pasted_path = tmp_path / "exposed" / "paste.kicad_mod"
    libwriter.write_footprint(
        spec,
        compute_land_pattern(spec),
        pasted_path,
        spec_sha256=part_spec_sha256(spec_path),
        ep_paste_margin_mm=-0.05,
    )
    parsed = parse_footprint(pasted_path)
    margins = {pad.number: pad.paste_margin for pad in parsed.pads}
    assert margins["17"] == -0.05
    assert margins["18"] == -0.05

    _fixture, spec, spec_path, _library, _footprint, _model = _make_fixture(
        tmp_path / "tab",
        _sot223_spec(),
        monkeypatch,
    )
    tab_path = tmp_path / "tab" / "tab.kicad_mod"
    libwriter.write_footprint(
        spec,
        compute_land_pattern(spec),
        tab_path,
        spec_sha256=part_spec_sha256(spec_path),
        ep_paste_margin_mm=0.05,
    )
    tab_pad = next(pad for pad in parse_footprint(tab_path).pads if pad.number == "4")
    assert tab_pad.paste_margin is None
