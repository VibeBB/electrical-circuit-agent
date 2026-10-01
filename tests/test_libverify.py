import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from circuit import libverify as libverify_module
from circuit.datasheet import DatasheetExtraction
from circuit.landpattern import LandPatternResult, compute_land_pattern
from circuit.libitems import FootprintDef, PadDef, parse_footprint
from circuit.libsource import (
    LibraryProvenance,
    LicenseInfo,
    ProvenanceEntry,
    SourceInfo,
)
from circuit.libverify import LibraryVerification, verify_library_part
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
    PartSpecReport,
    PinSpec,
    PinTable,
    Reading,
    part_spec_sha256,
)

PadTransform = Callable[[LandPad], tuple[str, float, float, float, float, float]]


def _reading(text: str = "mechanical evidence") -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="vision.json")


@pytest.fixture(autouse=True)
def _fresh_part_spec_check(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path="part.pdf",
        pdf_sha256="b" * 64,
        page_count=0,
        pages=[],
        tools={},
    )

    def check(
        _spec: PartSpec,
        _extraction: DatasheetExtraction,
        *,
        spec_path: Path,
        extraction_path: Path,
    ) -> PartSpecReport:
        return PartSpecReport(
            artifact_kind="circuit_part_spec_check",
            verdict="pass",
            part_spec_sha256=part_spec_sha256(spec_path),
            extraction_sha256="c" * 64,
            pdf_sha256="b" * 64,
            checked_readings=1,
            findings=[],
        )

    monkeypatch.setattr(libverify_module, "check_part_spec", check)

    def load(_path: Path) -> DatasheetExtraction:
        return extraction

    monkeypatch.setattr(libverify_module, "load_extraction", load)


def _dimension(
    nominal: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> Dimension:
    return Dimension(
        min=minimum,
        nom=nominal,
        max=maximum,
        reading=_reading(str(nominal)),
    )


def _pad_change(
    *,
    rename_from: str | None = None,
    rename_to: str | None = None,
    x_offsets: dict[str, float] | None = None,
    heights: dict[str, float] | None = None,
) -> PadTransform:
    x_offsets = {} if x_offsets is None else x_offsets
    heights = {} if heights is None else heights

    def transform(pad: LandPad) -> tuple[str, float, float, float, float, float]:
        number = rename_to if pad.number == rename_from and rename_to is not None else pad.number
        return (
            number,
            pad.x + x_offsets.get(pad.number, 0.0),
            pad.y,
            pad.width,
            heights.get(pad.number, pad.height),
            0.0,
        )

    return transform


def _dual_spec() -> PartSpec:
    package = PackageSpec(
        family="gullwing_dual",
        drawing_id="SOIC-4",
        pin_count=4,
        pitch=_dimension(1.27),
        body_length=_dimension(4.0),
        body_width=_dimension(2.0),
        height=_dimension(1.0),
        lead_span=_dimension(4.0),
        lead_length=_dimension(0.5),
        lead_width=_dimension(0.3),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin one top left"),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST62130",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number=str(number),
                name=f"PIN{number}",
                electrical_type="passive",
                reading=_reading(f"{number} PIN{number}"),
            )
            for number in range(1, 5)
        ],
        orderable=[
            OrderableVariant(
                mpn="TEST62130",
                package_designator="SOIC-4",
                pin_count=4,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TEST62130 SOIC-4"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _vqfn_spec(*, pitch: float = 0.5) -> PartSpec:
    pads: list[LandPad] = []
    number = 1
    positions = [-0.75, -0.25, 0.25, 0.75]
    for y in positions:
        pads.append(
            LandPad(
                number=str(number),
                x=-1.4,
                y=y,
                width=0.6,
                height=0.24,
                shape="roundrect",
            )
        )
        number += 1
    for x in positions:
        pads.append(
            LandPad(
                number=str(number),
                x=x,
                y=1.4,
                width=0.24,
                height=0.6,
                shape="roundrect",
            )
        )
        number += 1
    for y in reversed(positions):
        pads.append(
            LandPad(
                number=str(number),
                x=1.4,
                y=y,
                width=0.6,
                height=0.24,
                shape="roundrect",
            )
        )
        number += 1
    for x in reversed(positions):
        pads.append(
            LandPad(
                number=str(number),
                x=x,
                y=-1.4,
                width=0.24,
                height=0.6,
                shape="roundrect",
            )
        )
        number += 1
    pads.append(
        LandPad(
            number="17",
            x=0,
            y=0,
            width=1.68,
            height=1.68,
            shape="rect",
        )
    )
    package = PackageSpec(
        family="no_lead_quad",
        drawing_id="VQFN-16-1EP",
        pin_count=16,
        pitch=_dimension(pitch),
        body_length=_dimension(3.0, minimum=2.9, maximum=3.1),
        body_width=_dimension(3.0, minimum=2.9, maximum=3.1),
        height=_dimension(0.8),
        lead_length=_dimension(0.45),
        lead_width=_dimension(0.30, minimum=0.18, maximum=0.30),
        exposed_pad=ExposedPad(
            number="17",
            length=_dimension(1.68, minimum=1.61, maximum=1.75),
            width=_dimension(1.68, minimum=1.61, maximum=1.75),
        ),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin one top left"),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TESTVQFN16",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="b" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        land_pattern=LandPattern(
            source="datasheet",
            dimensions={"pitch": _dimension(pitch)},
            pads=pads,
        ),
        pins=[
            PinSpec(
                number=str(pin_number),
                name=f"PIN{pin_number}",
                electrical_type="passive",
                reading=_reading(f"{pin_number} PIN{pin_number}"),
            )
            for pin_number in range(1, 18)
        ],
        orderable=[
            OrderableVariant(
                mpn="TESTVQFN16",
                package_designator="VQFN-16-1EP",
                pin_count=16,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TESTVQFN16 VQFN-16-1EP"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _symbol_text(
    spec: PartSpec,
    *,
    pin_numbers: list[str] | None = None,
    name_overrides: dict[str, str] | None = None,
    type_overrides: dict[str, str] | None = None,
    x_overrides: dict[str, float] | None = None,
    properties: dict[str, str] | None = None,
) -> str:
    pin_numbers = pin_numbers or [pin.number for pin in spec.pins]
    name_overrides = name_overrides or {}
    type_overrides = type_overrides or {}
    x_overrides = x_overrides or {}
    properties = properties or {
        "Reference": "U",
        "Value": spec.mpn,
        "Footprint": "Fixture:SOIC-4",
        "Datasheet": "part.pdf",
    }
    property_text = "\n".join(f'(property "{key}" "{value}")' for key, value in properties.items())
    pins: list[str] = []
    for number in pin_numbers:
        spec_pin = next((pin for pin in spec.pins if pin.number == number), None)
        name = name_overrides.get(number, spec_pin.name if spec_pin is not None else f"PIN{number}")
        pin_type = type_overrides.get(number, spec_pin.electrical_type if spec_pin else "passive")
        x = x_overrides.get(number, 0.0)
        pins.append(
            f'(pin {pin_type} line (at {x} 0 0) (length 2.54) (name "{name}") (number "{number}"))'
        )
    return (
        "(kicad_symbol_lib (version 20241209) "
        f'(symbol "{spec.mpn}" {property_text} '
        f'(symbol "{spec.mpn}_0_1" {" ".join(pins)})))'
    )


def _footprint_text(
    name: str,
    reference: LandPatternResult,
    spec: PartSpec,
    *,
    pad_transform: PadTransform | None = None,
    attribute: bool = True,
    courtyard: bool = True,
    courtyard_scale: float = 1.0,
    fab_shift: float = 0.0,
    silk_at: tuple[float, float] | None = None,
    model: str | None = "${TEST_3DMODEL_DIR}/fixture.step",
    pad_type_number: str | None = None,
) -> str:
    lines = [f'(footprint "{name}" (layer "F.Cu")']
    if attribute:
        lines.append("(attr smd)")
    lines.extend(
        [
            '(property "Reference" "U**")',
            f'(property "Value" "{name}")',
            f'(property "Footprint" "Fixture:{name}")',
            '(property "Datasheet" "part.pdf")',
        ]
    )
    body_width = float(spec.package.body_width.nom or 0.0)
    body_length = float(spec.package.body_length.nom or 0.0)
    lines.append(
        f"(fp_rect (start {-body_width / 2 + fab_shift} {-body_length / 2 + fab_shift}) "
        f"(end {body_width / 2 + fab_shift} {body_length / 2 + fab_shift}) "
        '(stroke (width 0.05) (type solid)) (fill none) (layer "F.Fab"))'
    )
    if courtyard:
        x0, y0, x1, y1 = reference.courtyard
        center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
        half_width, half_height = (x1 - x0) * courtyard_scale / 2, (y1 - y0) * courtyard_scale / 2
        x0, x1 = center_x - half_width, center_x + half_width
        y0, y1 = center_y - half_height, center_y + half_height
        lines.append(
            f"(fp_rect (start {x0} {y0}) (end {x1} {y1}) "
            '(stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))'
        )
    if silk_at is not None:
        x, y = silk_at
        lines.append(
            f"(fp_line (start {x - 0.1} {y}) (end {x + 0.1} {y}) "
            '(stroke (width 0.05) (type solid)) (layer "F.SilkS"))'
        )
    for pad in reference.pads:
        number, x, y, width, height, rotation = (
            pad_transform(pad)
            if pad_transform is not None
            else (pad.number, pad.x, pad.y, pad.width, pad.height, 0.0)
        )
        layers = '"F.Cu" "F.Mask" "F.Paste"'
        drill = ""
        pad_type = "thru_hole" if number == pad_type_number else "smd"
        if pad_type == "thru_hole":
            drill = "(drill 0.5) "
            layers = '"*.Cu" "*.Mask"'
        lines.append(
            f'(pad "{number}" {pad_type} {pad.shape} (at {x} {y} {rotation}) '
            f"(size {width} {height}) {drill}(layers {layers}))"
        )
    if model is not None:
        lines.append(f'(model "{model}")')
    lines.append(")")
    return "\n".join(lines)


def _write_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: PartSpec | None = None,
    *,
    pad_transform: PadTransform | None = None,
    symbol_kwargs: dict[str, object] | None = None,
    footprint_kwargs: dict[str, object] | None = None,
) -> tuple[PartSpec, LandPatternResult, Path, Path, Path, Path]:
    spec = _dual_spec() if spec is None else spec
    reference = compute_land_pattern(spec)
    symbol_dir = tmp_path / "library"
    symbol_dir.mkdir(parents=True, exist_ok=True)
    symbol_path = symbol_dir / "Fixture.kicad_sym"
    footprint_dir = symbol_dir / "Fixture.pretty"
    footprint_dir.mkdir(parents=True, exist_ok=True)
    footprint_path = footprint_dir / f"{spec.package.drawing_id}.kicad_mod"
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "fixture.step").write_text("ISO-10303-21;", encoding="utf-8")
    monkeypatch.setenv("TEST_3D_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("TEST_3DMODEL_DIR", str(tmp_path / "models"))
    _fake_cli(tmp_path, monkeypatch)
    symbol_args = cast(dict[str, Any], symbol_kwargs or {})
    footprint_args = cast(dict[str, Any], footprint_kwargs or {})
    symbol_path.write_text(_symbol_text(spec, **symbol_args), encoding="utf-8")
    footprint_path.write_text(
        _footprint_text(
            spec.package.drawing_id,
            reference,
            spec,
            pad_transform=pad_transform,
            **footprint_args,
        ),
        encoding="utf-8",
    )
    spec_path = tmp_path / "part-spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    report_path = tmp_path / "part-spec-check.json"
    return spec, reference, spec_path, report_path, symbol_path, footprint_path


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int = 0) -> None:
    script = tmp_path / "fake-kicad-cli"
    script.write_text(
        f"#!/bin/sh\n"
        'if [ -e "$5" ]; then\n'
        "  echo 'output path must not exist before upgrade' >&2\n"
        "  exit 3\n"
        "fi\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(script))


def _verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: PartSpec | None = None,
    pad_transform: PadTransform | None = None,
    symbol_kwargs: dict[str, object] | None = None,
    footprint_kwargs: dict[str, object] | None = None,
    model_required: bool = True,
    library_dir: Path | None = None,
) -> tuple[LibraryVerification, tuple[PartSpec, LandPatternResult, Path, Path, Path, Path]]:
    case = _write_case(
        tmp_path,
        monkeypatch,
        spec,
        pad_transform=pad_transform,
        symbol_kwargs=symbol_kwargs,
        footprint_kwargs=footprint_kwargs,
    )
    part_spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    report = verify_library_part(
        part_spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=part_spec.mpn,
        footprint_path=footprint_path,
        library_dir=library_dir,
        reference=reference,
        model_required=model_required,
    )
    return report, case


def _codes(report: LibraryVerification) -> set[str]:
    return {finding.code for finding in report.findings}


def test_valid_library_part_passes_and_writes_default_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, case = _verify(tmp_path, monkeypatch)
    footprint_path = case[-1]

    assert report.verdict == "pass"
    assert report.symbol.sha256 == hashlib.sha256(case[-2].read_bytes()).hexdigest()
    assert report.footprint.sha256 == hashlib.sha256(footprint_path.read_bytes()).hexdigest()
    assert report.models[0].resolved
    assert (footprint_path.parent / "verification" / "SOIC-4.verification.json").is_file()


def test_part_spec_must_pass_fresh_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, case = _verify(tmp_path, monkeypatch)
    spec_path, _, symbol_path, footprint_path = case[2:]
    failed_check = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="fail",
        part_spec_sha256=part_spec_sha256(spec_path),
        extraction_sha256="c" * 64,
        pdf_sha256="b" * 64,
        checked_readings=1,
        findings=[],
    )

    def fail_check(
        _spec: PartSpec,
        _extraction: DatasheetExtraction,
        *,
        spec_path: Path,
        extraction_path: Path,
    ) -> PartSpecReport:
        return failed_check

    monkeypatch.setattr(
        libverify_module,
        "check_part_spec",
        fail_check,
    )
    report = verify_library_part(
        case[0],
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=case[0].mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=case[1],
    )
    assert "part_spec_unchecked" in _codes(report)


def test_supplied_part_spec_check_is_bound_to_current_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    spec, reference, spec_path, check_path, symbol_path, footprint_path = case
    check = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="pass",
        part_spec_sha256=part_spec_sha256(spec_path),
        extraction_sha256="c" * 64,
        pdf_sha256="b" * 64,
        checked_readings=1,
        findings=[],
    )
    check_path.write_text(check.model_dump_json(), encoding="utf-8")

    report = verify_library_part(
        spec,
        spec_path=spec_path,
        spec_check_path=check_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )
    assert report.verdict == "pass"

    check.part_spec_sha256 = "d" * 64
    check_path.write_text(check.model_dump_json(), encoding="utf-8")
    stale = verify_library_part(
        spec,
        spec_path=spec_path,
        spec_check_path=check_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )
    assert "part_spec_unchecked" in _codes(stale)


def test_kicad_cli_failures_are_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, case = _verify(tmp_path, monkeypatch)
    spec_path, _, symbol_path, footprint_path = case[2:]

    _fake_cli(tmp_path, monkeypatch, exit_code=1)
    failed_cli = verify_library_part(
        case[0],
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=case[0].mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=case[1],
    )
    assert "kicad_parse" in _codes(failed_cli)

    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    missing_cli = verify_library_part(
        case[0],
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=case[0].mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=case[1],
    )
    assert "kicad_cli_unavailable" in _codes(missing_cli)
    assert report.verdict == "pass"


@pytest.mark.parametrize(
    ("symbol_kwargs", "expected_code"),
    [
        ({"pin_numbers": ["1", "2", "3"]}, "symbol_pin_set"),
        ({"name_overrides": {"1": "WRONG"}}, "symbol_pin_name"),
        ({"type_overrides": {"1": "input"}}, "symbol_pin_type"),
        ({"x_overrides": {"1": 0.63}}, "symbol_pin_grid"),
        (
            {
                "properties": {
                    "Reference": "",
                    "Value": "X",
                    "Footprint": "Fixture:SOIC-4",
                    "Datasheet": "x",
                }
            },
            "symbol_property",
        ),
    ],
)
def test_symbol_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symbol_kwargs: dict[str, object],
    expected_code: str,
) -> None:
    report, _ = _verify(tmp_path, monkeypatch, symbol_kwargs=symbol_kwargs)
    assert expected_code in _codes(report)


def test_symbol_footprint_property_must_match_library_nickname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, case = _verify(
        tmp_path,
        monkeypatch,
        symbol_kwargs={
            "properties": {
                "Reference": "U",
                "Value": "Test",
                "Footprint": "Wrong:Footprint",
                "Datasheet": "part.pdf",
            }
        },
        library_dir=tmp_path / "library",
    )
    assert "symbol_property" in _codes(report)
    assert case[0].package.drawing_id == "SOIC-4"


@pytest.mark.parametrize(
    ("pad_change", "footprint_kwargs", "expected_codes"),
    [
        (
            _pad_change(rename_from="4", rename_to="9"),
            {},
            {"pad_set", "pin_pad_mapping"},
        ),
        (
            _pad_change(x_offsets={"2": 0.1}),
            {},
            {"pad_geometry"},
        ),
        (
            _pad_change(),
            {"pad_type_number": "1"},
            {"pad_type"},
        ),
        (
            _pad_change(),
            {"attribute": False},
            {"fp_attribute"},
        ),
        (
            _pad_change(x_offsets={"1": 2.0}),
            {},
            {"pin1_location"},
        ),
        (
            _pad_change(),
            {"courtyard": False},
            {"courtyard_missing", "courtyard_enclosure"},
        ),
        (
            _pad_change(),
            {"courtyard_scale": 0.0},
            {"courtyard_enclosure"},
        ),
        (
            _pad_change(),
            {"fab_shift": 0.2},
            {"fab_outline"},
        ),
        (
            _pad_change(heights={str(number): 1.35 for number in range(1, 5)}),
            {},
            {"pad_clearance"},
        ),
        (
            _pad_change(heights={str(number): 1.14 for number in range(1, 5)}),
            {},
            {"pad_clearance"},
        ),
        (
            _pad_change(heights={str(number): 0.15 for number in range(1, 5)}),
            {},
            {"lead_width_exceeds_pad"},
        ),
        (
            _pad_change(x_offsets={"1": 0.5}),
            {},
            {"lead_outside_pad"},
        ),
        (
            _pad_change(),
            {"silk_at": (-1.75, -0.635)},
            {"silk_over_pad"},
        ),
    ],
)
def test_footprint_and_geometry_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pad_change: PadTransform,
    footprint_kwargs: dict[str, object],
    expected_codes: set[str],
) -> None:
    report, _ = _verify(
        tmp_path,
        monkeypatch,
        pad_transform=pad_change,
        footprint_kwargs=footprint_kwargs,
    )
    assert expected_codes <= _codes(report)


def test_model_and_tolerance_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_model, _ = _verify(
        tmp_path / "missing-model", monkeypatch, footprint_kwargs={"model": None}
    )
    assert "model_missing" in _codes(missing_model)

    unresolved_model, _ = _verify(
        tmp_path / "unresolved",
        monkeypatch,
        footprint_kwargs={"model": "${UNDEFINED_MODEL}/fixture.step"},
    )
    assert "model_unresolved" in _codes(unresolved_model)

    no_required_model, _ = _verify(
        tmp_path / "optional",
        monkeypatch,
        footprint_kwargs={"model": None},
        model_required=False,
    )
    assert no_required_model.verdict == "pass"
    assert "model_missing" not in _codes(no_required_model)


def test_through_hole_packages_require_through_hole_pads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, case = _verify(tmp_path, monkeypatch)
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    package = spec.package.model_copy(update={"family": "through_hole_inline"})
    through_hole_spec = spec.model_copy(update={"package": package})
    spec_path.write_text(through_hole_spec.model_dump_json(), encoding="utf-8")

    report = verify_library_part(
        through_hole_spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )

    assert "pad_type" in _codes(report)


def test_provenance_missing_sha_mismatch_and_restricted_redistribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _dual_spec()
    reference = compute_land_pattern(spec)
    library_dir = tmp_path / "project" / "library"
    library_dir.mkdir(parents=True)
    _fake_cli(tmp_path, monkeypatch)
    symbol_path = library_dir / "Fixture.kicad_sym"
    footprint_dir = library_dir / "Fixture.pretty"
    footprint_dir.mkdir()
    footprint_path = footprint_dir / "SOIC-4.kicad_mod"
    symbol_path.write_text(_symbol_text(spec), encoding="utf-8")
    footprint_path.write_text(
        _footprint_text("SOIC-4", reference, spec),
        encoding="utf-8",
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "fixture.step").write_text("model", encoding="utf-8")
    monkeypatch.setenv("TEST_3DMODEL_DIR", str(tmp_path / "models"))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    reference_result = compute_land_pattern(spec)
    no_provenance = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=library_dir,
        reference=reference_result,
    )
    assert "provenance_missing" in _codes(no_provenance)
    assert "symbol_property" not in _codes(no_provenance)

    license_info = LicenseInfo(
        spdx="MIT",
        attribution="Example",
        redistribution="project_only",
    )
    source = SourceInfo(
        origin="generated",
        vendor="Example",
        license=license_info,
        original_path="source",
        original_sha256="f" * 64,
    )
    provenance = LibraryProvenance(
        entries=[
            ProvenanceEntry(
                artifact="symbol",
                name=spec.mpn,
                path="Fixture.kicad_sym",
                sha256="0" * 64,
                source=source,
            ),
            ProvenanceEntry(
                artifact="footprint",
                name="SOIC-4",
                path="Fixture.pretty/SOIC-4.kicad_mod",
                sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
                source=source,
            ),
        ]
    )
    (library_dir / "provenance.json").write_text(
        provenance.model_dump_json(indent=2),
        encoding="utf-8",
    )
    verified = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=library_dir,
        reference=reference_result,
    )
    assert "provenance_sha_mismatch" in _codes(verified)
    assert "redistribution_review" in _codes(verified)


def _vqfn_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pitch: float = 0.5,
    mutation: str | None = None,
) -> tuple[LibraryVerification, PartSpec]:
    spec = _vqfn_spec(pitch=pitch)
    reference = compute_land_pattern(spec)
    case = _write_case(tmp_path, monkeypatch, spec=spec)
    part_spec, _, spec_path, _check_path, symbol_path, footprint_path = case
    footprint = parse_footprint(footprint_path)
    pads: list[PadDef] = list(footprint.pads)
    if mutation == "mirrored":
        for pad in pads:
            if pad.number == "1":
                pad.y = 0.75
            elif pad.number == "4":
                pad.y = -0.75
    elif mutation == "pin1_top_right":
        for pad in pads:
            if pad.number == "1":
                pad.x = 1.4
    elif mutation == "ep_number":
        for pad in pads:
            if pad.number == "17":
                pad.number = "18"
    elif mutation == "ep_small":
        for pad in pads:
            if pad.number == "17":
                pad.width = 0.5
                pad.height = 0.5
    elif mutation == "pitch_065":
        for pad in pads:
            if pad.number in {"1", "2", "3", "4"}:
                pad.y = (int(pad.number) - 2.5) * 0.65
    if mutation is not None:
        _rewrite_footprint(footprint_path, footprint, pads)
    report = verify_library_part(
        part_spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=part_spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        model_required=True,
    )
    return report, part_spec


def _rewrite_footprint(path: Path, footprint: FootprintDef, pads: list[PadDef]) -> None:
    lines = [f'(footprint "{footprint.name}" (layer "F.Cu") (attr smd)']
    for pad in pads:
        lines.append(
            f'(pad "{pad.number}" smd roundrect (at {pad.x} {pad.y}) '
            f'(size {pad.width} {pad.height}) (layers "F.Cu" "F.Mask" "F.Paste"))'
        )
    lines.append('(model "${TEST_3DMODEL_DIR}/fixture.step")')
    lines.append(")")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_correct_vqfn_fixture_and_regression_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correct, _ = _vqfn_case(tmp_path / "correct", monkeypatch)
    assert correct.verdict == "pass"

    mirrored, _ = _vqfn_case(tmp_path / "mirrored", monkeypatch, mutation="mirrored")
    assert "pad_geometry" in _codes(mirrored)
    assert "pin1_location" in _codes(mirrored)

    pin1, _ = _vqfn_case(tmp_path / "pin1", monkeypatch, mutation="pin1_top_right")
    assert "pin1_location" in _codes(pin1)

    ep_number, _ = _vqfn_case(tmp_path / "ep", monkeypatch, mutation="ep_number")
    assert "pad_set" in _codes(ep_number)

    ep_size, _ = _vqfn_case(tmp_path / "ep-size", monkeypatch, mutation="ep_small")
    assert "exposed_pad_size" in _codes(ep_size)

    pitch, _ = _vqfn_case(tmp_path / "pitch", monkeypatch, mutation="pitch_065")
    assert "pad_geometry" in _codes(pitch)
