import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from PIL import Image
from pydantic import ValidationError

from circuit import authoring, occt, visionread
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
from circuit.libtestboard import TestBoard, TestBoardFinding
from circuit.libverify import LibraryVerification, inspect_model_file, verify_library_part
from circuit.lineage import (
    FootprintBase,
    FootprintLineage,
    PadChange,
    lineage_path_for,
    pad_changes,
)
from circuit.model3d import generate_model
from circuit.modeloracle import ModelExportReport
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
    SpecFinding,
    part_spec_sha256,
)
from circuit.pinsource import PinSourceInput
from circuit.ruleprofile import EffectiveRules, EvidenceRef, load_rules
from circuit.visionread import VisionBatch, VisionReadItem
from pinout_fixtures import QUAD16_NAMES, geometry_for_names, pinout_drawing
from test_datasheet import _pdf  # pyright: ignore[reportPrivateUsage]
from vision_fixtures import FIXTURE_IMPRESSION

PadTransform = Callable[[LandPad], tuple[str, float, float, float, float, float]]


def _reading(text: str = "mechanical evidence") -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="vision.json")


def _persist_vision_batch(batch_dir: Path, batch: VisionBatch) -> VisionBatch:
    real_fields = [item.field for item in batch.items if not item.control]
    if not real_fields:
        raise AssertionError("vision fixture needs a real read")
    control_field = real_fields[0]
    items = [
        item.model_copy(update={"field": control_field}) if item.control else item
        for item in batch.items
    ]
    field_bindings = {
        hashlib.sha256(f"{batch.control_salt}{item.read_id}".encode()).hexdigest(): item.field
        for item in items
    }
    batch = batch.model_copy(update={"items": items, "field_bindings": field_bindings})
    return visionread._write_batch(batch_dir, batch)  # pyright: ignore[reportPrivateUsage]


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
        spec: PartSpec,
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
            pinout=(
                geometry_for_names(
                    spec.pinout.labels_vision,
                    pin_count=spec.package.pin_count,
                    topology="quad" if spec.package.family == "no_lead_quad" else "dual",
                    page=spec.pinout.page,
                )
                if spec.pinout is not None
                else None
            ),
        )

    monkeypatch.setattr(libverify_module, "check_part_spec", check)

    def load(_path: Path) -> DatasheetExtraction:
        return extraction

    monkeypatch.setattr(libverify_module, "load_extraction", load)


@pytest.fixture(autouse=True)
def _stub_testboard_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    def build_testboard(*_args: object, **_kwargs: object) -> TestBoard:
        return TestBoard(
            artifact_kind="circuit_test_board",
            verdict="pass",
            clearance_mm=0.15,
            project_sha256="0" * 64,
        )

    monkeypatch.setattr(
        libverify_module,
        "build_test_board",
        build_testboard,
    )


@pytest.fixture(autouse=True)
def _stub_model_export_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(*_args: object, **_kwargs: object) -> ModelExportReport:
        return ModelExportReport(
            verdict="pass",
            model_sha256=None,
            runs=[],
            findings=[],
        )

    monkeypatch.setattr(libverify_module, "verify_model_export", verify)


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
        pitch=_dimension(1.28),
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
        pinout=pinout_drawing({str(number): f"PIN{number}" for number in range(1, 5)}),
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
        lead_length=_dimension(0.4),
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
    pin_names = {**QUAD16_NAMES, "17": "EP"}
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
        pinout=pinout_drawing(QUAD16_NAMES),
        land_pattern=LandPattern(
            source="datasheet",
            dimensions={"pitch": _dimension(pitch)},
            pads=pads,
        ),
        pins=[
            PinSpec(
                number=str(pin_number),
                name=pin_names[str(pin_number)],
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
    for index, number in enumerate(pin_numbers):
        spec_pin = next((pin for pin in spec.pins if pin.number == number), None)
        name = name_overrides.get(number, spec_pin.name if spec_pin is not None else f"PIN{number}")
        pin_type = type_overrides.get(number, spec_pin.electrical_type if spec_pin else "passive")
        x = x_overrides.get(number, float(index) * 2.54)
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


def _ensure_package_identity_pdf(tmp_path: Path, spec: PartSpec) -> PartSpec:
    relative_path = Path(spec.datasheet.path)
    if relative_path.is_absolute():
        return spec
    pdf_path = tmp_path / relative_path
    if pdf_path.is_file():
        return spec
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    body_nominals = [
        dimension.nom
        for dimension in (spec.package.body_length, spec.package.body_width)
        if dimension.nom is not None
    ]
    lower = max(0.01, min(body_nominals) - 0.25)
    upper = max(body_nominals) + 0.25
    table_commands = [
        "BT /F1 8 Tf 20 360 Td (Orderable Device) Tj ET",
        "BT /F1 8 Tf 175 360 Td (Status) Tj ET",
        "BT /F1 8 Tf 245 360 Td (Package Type) Tj ET",
        "BT /F1 8 Tf 320 360 Td (Package Drawing) Tj ET",
        "BT /F1 8 Tf 405 360 Td (Pins) Tj ET",
        "BT /F1 8 Tf 450 360 Td (Package Qty) Tj ET",
        f"BT /F1 8 Tf 20 340 Td ({spec.mpn}) Tj ET",
        "BT /F1 8 Tf 175 340 Td (ACTIVE) Tj ET",
        "BT /F1 8 Tf 245 340 Td (GEN) Tj ET",
        "BT /F1 8 Tf 320 340 Td (PKG) Tj ET",
        f"BT /F1 8 Tf 405 340 Td ({spec.package.pin_count}) Tj ET",
        "BT /F1 8 Tf 450 340 Td (3000) Tj ET",
    ]
    _pdf(
        pdf_path,
        [([], 0), (["PACKAGE OUTLINE", "PKG0001", f"{lower:.2f} {upper:.2f} mm"], 0)],
        page_size=(500, 500),
        extra_commands=[table_commands, []],
    )
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    return spec.model_copy(
        update={
            "datasheet": spec.datasheet.model_copy(
                update={"path": str(pdf_path.resolve()), "sha256": digest}
            )
        }
    )


def _write_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: PartSpec | None = None,
    *,
    pad_transform: PadTransform | None = None,
    symbol_kwargs: dict[str, object] | None = None,
    footprint_kwargs: dict[str, object] | None = None,
    stub_cli: bool = True,
    record_authoring: bool = True,
) -> tuple[PartSpec, LandPatternResult, Path, Path, Path, Path]:
    spec = _dual_spec() if spec is None else spec
    spec = _ensure_package_identity_pdf(tmp_path, spec)
    authoring_ref = Path("authoring") / "part" / "run-1"
    spec = spec.model_copy(update={"authoring": authoring_ref.as_posix()})
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
    if stub_cli:
        _fake_cli(tmp_path, monkeypatch)
    symbol_args = cast(dict[str, Any], symbol_kwargs or {})
    footprint_args = cast(dict[str, Any], dict(footprint_kwargs or {}))
    model_reference = f"${{TEST_3DMODEL_DIR}}/{spec.mpn}.3dshapes/{spec.package.drawing_id}.step"
    generate_fixture_model = "model" not in footprint_args and spec.package.family in {
        "chip",
        "gullwing_dual",
        "gullwing_quad",
        "no_lead_dual",
        "no_lead_quad",
    }
    footprint_args.setdefault("model", model_reference)
    symbol_path.write_text(_symbol_text(spec, **symbol_args), encoding="utf-8")
    if generate_fixture_model:
        source_dir = tmp_path / "model-source" / "Fixture.pretty"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / footprint_path.name
        source_path.write_text(
            _footprint_text(
                spec.package.drawing_id,
                reference,
                spec,
                model=model_reference,
            ),
            encoding="utf-8",
        )
        generate_model(spec, source_path, tmp_path / "models")
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
    run_dir = tmp_path / authoring_ref
    run_dir.mkdir(parents=True)
    if record_authoring:
        for lane, model in (("a", "model-a"), ("b", "model-b")):
            lane_dir = run_dir / lane
            lane_dir.mkdir()
            lane_spec_path = lane_dir / "part-spec.json"
            lane_spec_data = spec.model_dump(mode="python")

            def clear_vision_reads(value: Any) -> None:
                if isinstance(value, dict):
                    mapping = cast(dict[str, Any], value)
                    for key, child in mapping.items():
                        if key in {
                            "vision_read",
                            "labels_vision_read",
                            "orderable_vision_read",
                        }:
                            mapping[key] = None
                        else:
                            clear_vision_reads(child)
                elif isinstance(value, list):
                    for child in cast(list[Any], value):
                        clear_vision_reads(child)

            clear_vision_reads(lane_spec_data)
            lane_spec = PartSpec.model_validate(lane_spec_data).model_copy(
                update={"authoring": None}
            )
            _attach_authoring_read(lane_spec, lane_dir, lane)
            lane_spec_path.write_text(
                lane_spec.model_dump_json(indent=2),
                encoding="utf-8",
            )
            monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", lane)
            authoring.commit_lane(
                run_dir,
                lane_spec_path,
                profile=f"profile-{lane}",
                model=model,
                impression=FIXTURE_IMPRESSION,
            )
    _write_comparison_records(spec, spec_path, symbol_path, footprint_path)
    report_path = tmp_path / "part-spec-check.json"
    return spec, reference, spec_path, report_path, symbol_path, footprint_path


def _write_comparison_records(
    spec: PartSpec,
    spec_path: Path,
    symbol_path: Path,
    footprint_path: Path,
) -> None:
    spec_hash = part_spec_sha256(spec_path)
    comparisons: tuple[tuple[Literal["footprint", "symbol"], visionread.VisionKind, Path], ...] = (
        ("footprint", "compare_footprint", footprint_path),
        ("symbol", "compare_symbol", symbol_path),
    )
    for artifact_kind, kind, artifact_path in comparisons:
        batch_id = f"comparison-{artifact_kind}"
        batch_dir = spec_path.parent / "vision-reads" / batch_id
        image_path = batch_dir / "images" / "comparison.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 20), "white").save(image_path, format="PNG")
        image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
        salt = f"{artifact_kind}-salt"
        read_id = f"{artifact_kind}-read"
        control_id = f"{artifact_kind}-control"
        comparison = cast(
            visionread.VisionComparison,
            {
                "pin1_matches": True,
                "arrangement_matches": True,
                "numbering_direction_matches": True,
                "differences": [],
            },
        )
        comparison_answer = json.dumps(comparison)
        prompt = visionread.prompt_for_kind(kind)
        bindings = {
            "part_spec_sha256": spec_hash,
            "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "artifact_kind": artifact_kind,
        }
        items = [
            VisionReadItem(
                read_id=read_id,
                field=f"library.{artifact_kind}",
                kind=kind,
                page=1,
                bbox=(1.0, 1.0, 10.0, 10.0),
                crop_bbox=(0.0, 0.0, 12.0, 12.0),
                dpi=300,
                rasterizer="pdftoppm",
                image_path="images/comparison.png",
                image_sha256=image_sha,
                prompt=prompt,
                prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                bindings=bindings,
            ),
            VisionReadItem(
                read_id=control_id,
                field="control",
                kind=kind,
                page=1,
                bbox=(1.0, 1.0, 10.0, 10.0),
                crop_bbox=(0.0, 0.0, 12.0, 12.0),
                dpi=300,
                rasterizer="pdftoppm",
                image_path="images/comparison.png",
                image_sha256=image_sha,
                prompt=prompt,
                prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                control=True,
            ),
        ]
        batch = VisionBatch(
            artifact_kind="circuit_vision_read_batch",
            batch_id=batch_id,
            created_at="2026-01-01T00:00:00Z",
            lane="main",
            profile="test",
            model="test",
            pdf_path=spec.datasheet.path,
            pdf_sha256=spec.datasheet.sha256,
            items=items,
            control_salt=salt,
            control_answer_sha256=hashlib.sha256(f"{salt}CONTROL".encode()).hexdigest(),
            control_read_sha256=hashlib.sha256(f"{salt}{control_id}".encode()).hexdigest(),
        )
        answers = visionread.VisionAnswerRecord(
            artifact_kind="circuit_vision_read_answers",
            batch_id=batch_id,
            answered_at="2026-01-01T00:00:00Z",
            answers={read_id: comparison_answer, control_id: "CONTROL"},
            impressions={read_id: FIXTURE_IMPRESSION, control_id: FIXTURE_IMPRESSION},
            normalized={read_id: comparison, control_id: "CONTROL"},
            status={read_id: "ok", control_id: "ok"},
            control_passed=True,
        )
        _persist_vision_batch(batch_dir, batch)
        (batch_dir / "answers.json").write_text(
            answers.model_dump_json(indent=2),
            encoding="utf-8",
        )


def _attach_authoring_read(spec: PartSpec, lane_dir: Path, lane: str) -> None:
    batch_dir = lane_dir / "vision-reads" / f"fixture-{lane}"
    batch_dir.mkdir(parents=True)
    prompt = visionread.prompt_for_kind("transcribe")
    rasterizer: visionread.Rasterizer = "pdftoppm" if lane == "a" else "pdfium"
    control_id = f"control-{lane}"
    read_id = f"read-{lane}"
    control_answer = "ABC234"
    timestamp = "2025-01-01T00:00:00+00:00"

    def make_item(read_id: str, field: str, *, control: bool) -> visionread.VisionReadItem:
        image_path = batch_dir / f"{read_id}.png"
        Image.new("RGB", (1, 1), color="white").save(image_path, format="PNG")
        return visionread.VisionReadItem(
            read_id=read_id,
            field=field,
            kind="transcribe",
            page=1,
            bbox=(0, 0, 1, 1),
            crop_bbox=(0, 0, 1, 1),
            dpi=300,
            rasterizer=rasterizer,
            image_path=image_path.name,
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            prompt=prompt,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            control=control,
        )

    control_salt = f"fixture-salt-{lane}"
    batch = visionread.VisionBatch(
        artifact_kind="circuit_vision_read_batch",
        batch_id=f"fixture-{lane}",
        created_at=timestamp,
        lane=lane,
        profile=f"profile-{lane}",
        model=f"model-{lane}",
        pdf_path=spec.datasheet.path,
        pdf_sha256=spec.datasheet.sha256,
        items=[
            make_item(control_id, "control", control=True),
            make_item(read_id, "package.body_length", control=False),
        ],
        control_salt=control_salt,
        control_answer_sha256=hashlib.sha256(
            f"{control_salt}{control_answer.casefold()}".encode()
        ).hexdigest(),
        control_read_sha256=hashlib.sha256(f"{control_salt}{control_id}".encode()).hexdigest(),
    )
    answers = visionread.VisionAnswerRecord(
        artifact_kind="circuit_vision_read_answers",
        batch_id=batch.batch_id,
        answered_at=timestamp,
        answers={control_id: control_answer, read_id: "3.0"},
        impressions={
            control_id: FIXTURE_IMPRESSION,
            read_id: FIXTURE_IMPRESSION,
        },
        normalized={control_id: control_answer, read_id: "3.0"},
        status={control_id: "ok", read_id: "ok"},
        control_passed=True,
    )
    _persist_vision_batch(batch_dir, batch)
    (batch_dir / "answers.json").write_text(answers.model_dump_json(indent=2), encoding="utf-8")
    spec.package.body_length.reading.vision_read = (
        f"vision-reads/fixture-{lane}/batch.json#{read_id}"
    )


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


def _rewrite_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: PartSpec | None = None,
    mutation: Callable[[occt.Shape], occt.Shape] | None = None,
    model_update: dict[str, object] | None = None,
) -> tuple[LibraryVerification, Path]:
    case = _write_case(tmp_path, monkeypatch, spec=spec)
    part_spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    model_path = (
        tmp_path / "models" / f"{part_spec.mpn}.3dshapes" / f"{part_spec.package.drawing_id}.step"
    )
    shape = occt.read_step(model_path)
    if mutation is not None:
        shape = mutation(shape)
    occt.write_step(shape, model_path, product_name=model_path.stem)
    if model_update is not None:
        parsed = parse_footprint(footprint_path)
        updated_path = model_update.get("path")
        if isinstance(updated_path, str) and updated_path.casefold().endswith(
            (".wrl", ".wrz", ".stpz", ".igs")
        ):
            copy_path = Path(updated_path)
            if not copy_path.is_absolute():
                copy_path = footprint_path.parent / copy_path
            copy_path.write_bytes(model_path.read_bytes())
        updated_model = parsed.models[0].model_copy(update=model_update)
        updated_footprint = parsed.model_copy(update={"models": [updated_model]})

        def parse_updated_footprint(_path: Path) -> FootprintDef:
            return updated_footprint

        monkeypatch.setattr(
            libverify_module,
            "parse_footprint",
            parse_updated_footprint,
        )
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
    return report, model_path


def _model_body_and_terminals(shape: occt.Shape) -> tuple[list[occt.Shape], int]:
    solids = list(occt.solids(shape))
    facts = occt.inspect(shape)
    body_index = max(range(len(solids)), key=lambda index: facts.solids[index].volume)
    return solids, body_index


def test_verify_library_part_includes_testboard_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    spec, reference, spec_path, _report_path, symbol_path, footprint_path = case
    finding = TestBoardFinding(
        code="assembly_attribute",
        severity="error",
        subject="U1",
        message="footprint is excluded from position export",
    )
    board = TestBoard(
        artifact_kind="circuit_test_board",
        verdict="fail",
        clearance_mm=0.15,
        project_sha256="1" * 64,
        findings=[finding],
    )
    calls: list[tuple[object, ...]] = []

    def build(*args: object) -> TestBoard:
        calls.append(args)
        return board

    monkeypatch.setattr(libverify_module, "build_test_board", build)
    report = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        model_required=False,
        test_board=True,
    )

    assert calls
    assert report.inputs.test_board
    assert report.test_board == board
    assert "assembly_attribute" in _codes(report)
    assert report.verdict == "fail"


def _write_lineage_for_case(
    case: tuple[PartSpec, LandPatternResult, Path, Path, Path, Path],
    *,
    changes: list[PadChange] | None = None,
    footprint_sha256: str | None = None,
) -> tuple[Path, Path, Path]:
    spec, reference, _spec_path, _report_path, _symbol_path, footprint_path = case
    project_root = footprint_path.parent.parent
    base_path = project_root / "base.kicad_mod"
    base_path.write_text(
        _footprint_text(
            spec.package.drawing_id,
            reference,
            spec,
        ),
        encoding="utf-8",
    )
    evidence_path = project_root / "prototype.txt"
    evidence_path.write_text("prototype results", encoding="utf-8")
    base_footprint = parse_footprint(base_path)
    current_footprint = parse_footprint(footprint_path)
    recorded = pad_changes(base_footprint, current_footprint) if changes is None else changes
    lineage = FootprintLineage(
        artifact_kind="circuit_footprint_lineage",
        footprint_sha256=(
            hashlib.sha256(footprint_path.read_bytes()).hexdigest()
            if footprint_sha256 is None
            else footprint_sha256
        ),
        layer="organization",
        base=FootprintBase(
            kind="generated",
            path="base.kicad_mod",
            sha256=hashlib.sha256(base_path.read_bytes()).hexdigest(),
            rule_chain_sha256="e" * 64,
        ),
        changes=recorded or [PadChange(pad="1", field="x", before=0.0, after=0.1)],
        reason="prototype assembly evidence",
        evidence=[
            EvidenceRef(
                kind="prototype",
                path="prototype.txt",
                sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
        ],
    )
    sidecar = lineage_path_for(footprint_path)
    sidecar.write_text(lineage.model_dump_json(indent=2), encoding="utf-8")
    return base_path, evidence_path, sidecar


def _verify_case(
    case: tuple[PartSpec, LandPatternResult, Path, Path, Path, Path],
    *,
    rules: EffectiveRules | None = None,
) -> LibraryVerification:
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    return verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        rules=rules,
    )


def test_pad_diff_tracks_fields_and_pad_additions_removals() -> None:
    base = FootprintDef(
        name="base",
        attributes=[],
        pads=[
            PadDef(
                number="1",
                type="smd",
                shape="roundrect",
                x=0.0,
                y=0.0,
                rotation=0.0,
                width=1.0,
                height=0.5,
                drill=None,
                layers=["F.Cu"],
                roundrect_ratio=0.25,
                paste_margin=0.0,
                mask_margin=0.0,
            ),
            PadDef(
                number="2",
                type="smd",
                shape="rect",
                x=1.0,
                y=0.0,
                rotation=0.0,
                width=1.0,
                height=0.5,
                drill=None,
                layers=["F.Cu"],
            ),
        ],
        graphics=[],
        models=[],
        properties={},
    )
    current = FootprintDef(
        name="current",
        attributes=[],
        pads=[
            PadDef(
                number="1",
                type="smd",
                shape="rect",
                x=0.1,
                y=0.2,
                rotation=0.0,
                width=1.1,
                height=0.6,
                drill=None,
                layers=["F.Cu"],
                paste_margin=0.05,
                mask_margin=-0.05,
            ),
            PadDef(
                number="3",
                type="smd",
                shape="rect",
                x=2.0,
                y=0.0,
                rotation=0.0,
                width=0.5,
                height=0.5,
                drill=None,
                layers=["F.Cu"],
            ),
        ],
        graphics=[],
        models=[],
        properties={},
    )

    changes = pad_changes(base, current)
    assert {change.field for change in changes} == {
        "x",
        "y",
        "width",
        "height",
        "shape",
        "roundrect_ratio",
        "paste_margin",
        "mask_margin",
        "removed",
        "added",
    }


def test_pad_diff_uses_one_ten_thousandth_mm_tolerance() -> None:
    pad = PadDef(
        number="1",
        type="smd",
        shape="rect",
        x=0.0,
        y=0.0,
        rotation=0.0,
        width=1.0,
        height=0.5,
        drill=None,
        layers=["F.Cu"],
    )
    base = FootprintDef(
        name="base", attributes=[], pads=[pad], graphics=[], models=[], properties={}
    )
    within = FootprintDef(
        name="within",
        attributes=[],
        pads=[pad.model_copy(update={"x": 0.00009})],
        graphics=[],
        models=[],
        properties={},
    )
    outside = FootprintDef(
        name="outside",
        attributes=[],
        pads=[pad.model_copy(update={"x": 0.00011})],
        graphics=[],
        models=[],
        properties={},
    )

    assert pad_changes(base, within) == []
    assert [change.field for change in pad_changes(base, outside)] == ["x"]


def test_footprint_parser_reads_lineage_pad_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    footprint_path = case[-1]
    text = footprint_path.read_text(encoding="utf-8")
    text, replacements = re.subn(
        r'(\(pad "1".*?\(size [^)]+\))',
        r"\1 (roundrect_rratio 0.3) (solder_paste_margin 0.02) "
        r"(solder_mask_margin -0.01)",
        text,
        count=1,
    )
    assert replacements == 1
    footprint_path.write_text(text, encoding="utf-8")

    pad = next(pad for pad in parse_footprint(footprint_path).pads if pad.number == "1")

    assert pad.roundrect_ratio == 0.3
    assert pad.paste_margin == 0.02
    assert pad.mask_margin == -0.01


def test_lineage_models_require_generated_profile_hash_and_product_name() -> None:
    with pytest.raises(ValidationError):
        FootprintBase(
            kind="generated",
            path="base.kicad_mod",
            sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        FootprintLineage(
            artifact_kind="circuit_footprint_lineage",
            footprint_sha256="a" * 64,
            layer="product",
            base=FootprintBase(
                kind="manufacturer",
                path="base.kicad_mod",
                sha256="b" * 64,
            ),
            changes=[PadChange(pad="1", field="x", before=0.0, after=0.1)],
            reason="product tuning",
            evidence=[
                EvidenceRef(
                    kind="prototype",
                    path="prototype.txt",
                    sha256="c" * 64,
                )
            ],
        )


def test_valid_lineage_turns_covered_pad_geometry_into_intentional_tuning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(
        tmp_path,
        monkeypatch,
        pad_transform=_pad_change(x_offsets={"1": 0.05}),
    )
    _write_lineage_for_case(case)

    report = _verify_case(case)

    assert "pad_geometry" not in _codes(report)
    assert "intentional_tuning" in _codes(report)
    finding = next(item for item in report.findings if item.code == "intentional_tuning")
    assert finding.severity == "info"
    assert "reference_delta_mm" in finding.message
    assert "base_delta_mm" in finding.message


def test_recorded_tuning_does_not_relax_lead_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def shorten_pad(pad: LandPad) -> tuple[str, float, float, float, float, float]:
        return (
            pad.number,
            pad.x,
            pad.y,
            0.2 if pad.number == "1" else pad.width,
            pad.height,
            0.0,
        )

    case = _write_case(tmp_path, monkeypatch, pad_transform=shorten_pad)
    _write_lineage_for_case(case)

    report = _verify_case(case)

    assert "intentional_tuning" in _codes(report)
    assert "lead_outside_pad" in _codes(report)


def test_recorded_tuning_does_not_relax_ep_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _vqfn_spec()

    def enlarge_ep(pad: LandPad) -> tuple[str, float, float, float, float, float]:
        scale = 1.12 if pad.number == "17" else 1.0
        return (
            pad.number,
            pad.x,
            pad.y,
            pad.width * scale,
            pad.height * scale,
            0.0,
        )

    case = _write_case(tmp_path, monkeypatch, spec, pad_transform=enlarge_ep)
    _write_lineage_for_case(case)
    rules = load_rules("builtin:ipc7351b", Path(".")).model_copy(
        update={"min_ep_to_pad_clearance_mm": 0.2}
    )

    report = _verify_case(case, rules=rules)

    assert "intentional_tuning" in _codes(report)
    assert "ep_pad_clearance" in _codes(report)


def test_recorded_pad_position_swap_keeps_pinout_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _vqfn_spec()
    assert spec.land_pattern is not None
    positions = {pad.number: (pad.x, pad.y) for pad in spec.land_pattern.pads}

    def swap_pads(pad: LandPad) -> tuple[str, float, float, float, float, float]:
        swapped = {"1": "2", "2": "1"}.get(pad.number, pad.number)
        x, y = positions[swapped]
        return pad.number, x, y, pad.width, pad.height, 0.0

    case = _write_case(tmp_path, monkeypatch, spec, pad_transform=swap_pads)
    _write_lineage_for_case(case)

    report = _verify_case(case)

    assert "intentional_tuning" in _codes(report)
    assert "footprint_order_mismatch" in _codes(report) or (
        "footprint_chirality_mismatch" in _codes(report)
    )


def test_lineage_changes_must_match_current_pads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(
        tmp_path,
        monkeypatch,
        pad_transform=_pad_change(x_offsets={"1": 0.05}),
    )
    _write_lineage_for_case(
        case,
        changes=[PadChange(pad="2", field="x", before=0.0, after=0.05)],
    )

    report = _verify_case(case)

    assert {"lineage_unrecorded_change", "lineage_stale_change"} <= _codes(report)
    assert "pad_geometry" in _codes(report)
    assert "intentional_tuning" not in _codes(report)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("footprint", "lineage_footprint_hash"),
        ("base", "lineage_base"),
        ("evidence", "lineage_evidence"),
    ],
)
def test_lineage_hash_mismatches_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    code: str,
) -> None:
    case = _write_case(
        tmp_path,
        monkeypatch,
        pad_transform=_pad_change(x_offsets={"1": 0.05}),
    )
    base_path, evidence_path, sidecar = _write_lineage_for_case(case)
    if failure == "footprint":
        value = FootprintLineage.model_validate_json(sidecar.read_text(encoding="utf-8"))
        sidecar.write_text(
            value.model_copy(update={"footprint_sha256": "0" * 64}).model_dump_json(),
            encoding="utf-8",
        )
    elif failure == "base":
        base_path.write_text("modified base", encoding="utf-8")
    else:
        evidence_path.write_text("modified evidence", encoding="utf-8")

    report = _verify_case(case)

    assert code in _codes(report)
    assert "intentional_tuning" not in _codes(report)


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


def test_missing_hash_bound_comparison_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    spec_path = case[2]
    comparison_dir = spec_path.parent / "vision-reads" / "comparison-footprint"
    for path in comparison_dir.rglob("*"):
        if path.is_file():
            path.unlink()
    for path in sorted(comparison_dir.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()
    comparison_dir.rmdir()

    report = _verify_case(case)

    assert "vision_compare_missing" in _codes(report)
    assert report.verdict == "fail"


def test_stale_artifact_comparison_hash_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    footprint_path = case[-1]
    footprint_path.write_text(
        footprint_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    report = _verify_case(case)

    assert "vision_compare_stale" in _codes(report)


def test_failed_comparison_control_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    answers_path = case[2].parent / "vision-reads" / "comparison-footprint" / "answers.json"
    answers = visionread.VisionAnswerRecord.model_validate_json(
        answers_path.read_text(encoding="utf-8")
    )
    answers_path.write_text(
        answers.model_copy(update={"control_passed": False}).model_dump_json(indent=2),
        encoding="utf-8",
    )

    report = _verify_case(case)

    assert "vision_control_failed" in _codes(report)
    assert report.verdict == "fail"


def test_visual_mismatch_is_recorded_without_clearing_deterministic_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(
        tmp_path,
        monkeypatch,
        footprint_kwargs={"model": "${UNDEFINED_MODEL}/fixture.step"},
    )
    answers_path = case[2].parent / "vision-reads" / "comparison-footprint" / "answers.json"
    answers = visionread.VisionAnswerRecord.model_validate_json(
        answers_path.read_text(encoding="utf-8")
    )
    read_id = next(read_id for read_id in answers.answers if read_id.endswith("-read"))
    answers.normalized[read_id] = {
        "pin1_matches": False,
        "arrangement_matches": True,
        "numbering_direction_matches": True,
        "differences": ["pin 1 is on the opposite corner"],
    }
    answers_path.write_text(answers.model_dump_json(indent=2), encoding="utf-8")

    report = _verify_case(case)

    assert "vision_compare_mismatch" in _codes(report)
    assert "model_unresolved" in _codes(report)
    assert report.verdict == "fail"


def test_library_verification_enforces_authoring_consensus_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _report, case = _verify(tmp_path, monkeypatch)
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    changed = spec.model_copy(update={"manufacturer": "Different"})
    spec_path.write_text(changed.model_dump_json(indent=2), encoding="utf-8")

    report = verify_library_part(
        changed,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=changed.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )

    assert "authoring_consensus_violated" in _codes(report)
    missing = changed.model_copy(update={"authoring": None})
    report = verify_library_part(
        missing,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=missing.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )
    assert "authoring_missing" in _codes(report)


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
    assert "pinout_unverified" in _codes(report)


def test_fresh_part_spec_findings_are_included_in_library_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, case = _verify(tmp_path, monkeypatch)
    spec, reference, spec_path, check_path, symbol_path, footprint_path = case
    failed_check = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="fail",
        part_spec_sha256=part_spec_sha256(spec_path),
        extraction_sha256="c" * 64,
        pdf_sha256=spec.datasheet.sha256,
        checked_readings=1,
        findings=[
            SpecFinding(
                code="package_variant_unbound",
                severity="error",
                field="orderable",
                message="the PartSpec MPN must match exactly one orderable variant",
            )
        ],
    )
    check_path.write_text(failed_check.model_dump_json(indent=2), encoding="utf-8")

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

    assert "package_variant_unbound" in _codes(report)
    assert "part_spec_unchecked" in _codes(report)


def test_supplied_part_spec_check_is_bound_to_current_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    spec, reference, spec_path, check_path, symbol_path, footprint_path = case
    assert spec.pinout is not None
    check = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="pass",
        part_spec_sha256=part_spec_sha256(spec_path),
        extraction_sha256="c" * 64,
        pdf_sha256="b" * 64,
        checked_readings=1,
        findings=[],
        pinout=geometry_for_names(
            spec.pinout.labels_vision,
            pin_count=spec.package.pin_count,
            topology="dual",
            page=spec.pinout.page,
        ),
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


def test_package_identity_fails_closed_when_datasheet_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(tmp_path, monkeypatch)
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    missing_spec = spec.model_copy(
        update={
            "datasheet": spec.datasheet.model_copy(update={"path": str(tmp_path / "missing.pdf")})
        }
    )
    spec_path.write_text(missing_spec.model_dump_json(indent=2), encoding="utf-8")

    report = verify_library_part(
        missing_spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )

    finding = next(
        item for item in report.findings if item.code == "package_identity_verification_unavailable"
    )
    assert finding.subject == "datasheet"
    assert "missing.pdf" in finding.message
    assert "No such file" in finding.message


def test_package_identity_fails_closed_when_no_3d_model_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _write_case(
        tmp_path,
        monkeypatch,
        footprint_kwargs={"model": "${TEST_3DMODEL_DIR}/missing.step"},
    )
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case

    report = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
    )

    finding = next(
        item
        for item in report.findings
        if item.code == "package_identity_verification_unavailable" and item.subject == "model"
    )
    assert "missing.step" in finding.message
    assert "No such file" in finding.message


def test_independent_pin_source_comparison_and_single_source_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_source, case = _verify(tmp_path, monkeypatch)
    spec, reference, spec_path, _check_path, symbol_path, footprint_path = case
    assert any(
        item.code == "pin_source_single" and item.severity == "warning"
        for item in missing_source.findings
    )

    pin_source_path = tmp_path / "part.ibs"
    good_rows = [f"{pin.number} {pin.name} MODEL" for pin in spec.pins]
    pin_source_path.write_text(
        f"[Component]\n{spec.mpn}\n[Pin]\n" + "\n".join(good_rows) + "\n",
        encoding="utf-8",
    )
    matched = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        pin_source_path=pin_source_path,
    )
    assert matched.pin_source_comparison is not None
    assert matched.pin_source_comparison.passed
    assert not {
        "pin_source_missing",
        "pin_source_name_mismatch",
        "pin_source_invalid",
        "pin_source_single",
    } & _codes(matched)
    assert matched.inputs.pin_source_path is not None
    assert (
        matched.inputs.pin_source_sha256 == hashlib.sha256(pin_source_path.read_bytes()).hexdigest()
    )

    derived = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        pin_source_path=pin_source_path,
        pin_sources=[
            PinSourceInput(path=pin_source_path, derived_from=["part_spec"]),
        ],
    )
    assert derived.pin_source_comparison is not None
    assert derived.pin_source_comparison.independent_lineages == ["part_spec"]
    assert "pin_source_single" in _codes(derived)
    assert len(derived.inputs.pin_sources) == 1
    assert derived.inputs.pin_sources[0].derived_from == ["part_spec"]

    pin_source_path.write_text(
        "[Component]\nOTHER-PART\n[Pin]\n" + "\n".join(good_rows) + "\n",
        encoding="utf-8",
    )
    identity_mismatch = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        pin_source_path=pin_source_path,
    )
    assert "pin_source_identity_mismatch" in _codes(identity_mismatch)
    assert identity_mismatch.verdict == "fail"

    bad_rows = [f"{spec.pins[0].number} WRONG MODEL"]
    pin_source_path.write_text(
        f"[Component]\n{spec.mpn}\n[Pin]\n" + "\n".join(bad_rows) + "\n",
        encoding="utf-8",
    )
    mismatched = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        pin_source_path=pin_source_path,
    )
    assert {"pin_source_missing", "pin_source_name_mismatch"} <= _codes(mismatched)
    assert mismatched.verdict == "fail"

    pin_source_path.write_text("not a recognized pin source\n", encoding="utf-8")
    invalid = verify_library_part(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        library_dir=None,
        reference=reference,
        pin_source_path=pin_source_path,
    )
    assert "pin_source_invalid" in _codes(invalid)
    assert invalid.verdict == "fail"


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


def test_inspect_model_file_reports_missing_model(tmp_path: Path) -> None:
    report = inspect_model_file(
        _dual_spec(),
        Path(__file__).parent / "data" / "library" / "modern.kicad_mod",
        tmp_path / "missing.step",
    )

    assert report.verdict == "fail"
    assert report.sha256 is None
    assert [finding.code for finding in report.findings] == ["model_missing"]


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
    symbol_kwargs: dict[str, object] | None = None,
) -> tuple[LibraryVerification, PartSpec]:
    spec = _vqfn_spec(pitch=pitch)
    reference = compute_land_pattern(spec)
    lead_width_dimension = spec.package.lead_width
    if lead_width_dimension is None:
        raise AssertionError("VQFN fixture requires a lead width")
    lead_width = lead_width_dimension.nom
    if lead_width is None:
        raise AssertionError("VQFN fixture requires a nominal lead width")
    exposed_number = (
        spec.package.exposed_pad.number if spec.package.exposed_pad is not None else None
    )

    def terminal_width_pad(pad: LandPad) -> tuple[str, float, float, float, float, float]:
        if pad.number == exposed_number:
            return pad.number, pad.x, pad.y, pad.width, pad.height, 0.0
        if abs(pad.x) > abs(pad.y):
            width, height = pad.width, max(pad.height, lead_width)
        else:
            width, height = max(pad.width, lead_width), pad.height
        return pad.number, pad.x, pad.y, width, height, 0.0

    reference = reference.model_copy(
        update={
            "pads": [
                pad.model_copy(
                    update={
                        "width": terminal_width_pad(pad)[3],
                        "height": terminal_width_pad(pad)[4],
                    }
                )
                for pad in reference.pads
            ]
        }
    )
    case = _write_case(
        tmp_path,
        monkeypatch,
        spec=spec,
        pad_transform=terminal_width_pad,
        symbol_kwargs=symbol_kwargs,
    )
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
    elif mutation == "mirror_x":
        for pad in pads:
            pad.x = -pad.x
    elif mutation == "rotate_180":
        for pad in pads:
            pad.x, pad.y = -pad.x, -pad.y
    elif mutation == "swap_order":
        centers = {pad.number: (pad.x, pad.y) for pad in pads if pad.number in {"2", "3"}}
        for pad in pads:
            if pad.number == "2":
                pad.x, pad.y = centers["3"]
            elif pad.number == "3":
                pad.x, pad.y = centers["2"]
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
    model_path = (
        footprint.models[0].path if footprint.models else "${TEST_3DMODEL_DIR}/fixture.step"
    )
    lines.append(f'(model "{model_path}")')
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
    assert "footprint_rotation_mismatch" in _codes(pin1)

    ep_number, _ = _vqfn_case(tmp_path / "ep", monkeypatch, mutation="ep_number")
    assert "pad_set" in _codes(ep_number)

    ep_size, _ = _vqfn_case(tmp_path / "ep-size", monkeypatch, mutation="ep_small")
    assert "exposed_pad_size" in _codes(ep_size)

    pitch, _ = _vqfn_case(tmp_path / "pitch", monkeypatch, mutation="pitch_065")
    assert "pad_geometry" in _codes(pitch)


def test_model_geometry_rejects_mirrored_and_rotated_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirrored, model_path = _rewrite_model(
        tmp_path / "mirrored",
        monkeypatch,
        mutation=lambda shape: occt.transform(shape, mirror_x=True),
    )
    assert {"model_pin1_mismatch", "model_terminal_unmatched"} & _codes(mirrored)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    assert any(
        finding.model_sha256 == digest
        for finding in mirrored.findings
        if finding.code.startswith("model_")
    )

    rotated_90, _ = _rewrite_model(
        tmp_path / "rotated-90",
        monkeypatch,
        mutation=lambda shape: occt.transform(shape, rotation_z_deg=90),
    )
    assert {
        "model_terminal_unmatched",
        "model_pad_unmatched",
        "model_pitch",
    } & _codes(rotated_90)

    rotated_180, _ = _rewrite_model(
        tmp_path / "rotated-180",
        monkeypatch,
        spec=_vqfn_spec(),
        mutation=lambda shape: occt.transform(shape, rotation_z_deg=180),
    )
    assert "model_pin1_mismatch" in _codes(rotated_180)


def test_model_geometry_rejects_offset_pitch_and_unit_scale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offset, _ = _rewrite_model(
        tmp_path / "offset",
        monkeypatch,
        spec=_vqfn_spec(),
        mutation=lambda shape: occt.transform(shape, translation=(0.1, 0.0, 0.0)),
    )
    assert "model_terminal_outside_pad" in _codes(offset)

    def scale_terminals(shape: occt.Shape) -> occt.Shape:
        parts, body_index = _model_body_and_terminals(shape)
        return occt.compound(
            tuple(
                part if index == body_index else occt.transform(part, scale=1.02)
                for index, part in enumerate(parts)
            )
        )

    pitch, _ = _rewrite_model(
        tmp_path / "pitch",
        monkeypatch,
        spec=_vqfn_spec(),
        mutation=scale_terminals,
    )
    assert "model_pitch" in _codes(pitch)

    scaled, _ = _rewrite_model(
        tmp_path / "scaled",
        monkeypatch,
        mutation=lambda shape: occt.transform(shape, scale=25.4),
    )
    assert "model_body_dimension" in _codes(scaled)


def test_model_geometry_checks_overall_bottom_and_seated_height(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translated, _ = _rewrite_model(
        tmp_path / "translated-z",
        monkeypatch,
        spec=_vqfn_spec(),
        mutation=lambda shape: occt.transform(shape, translation=(0.0, 0.0, 0.2)),
    )
    assert "model_body_dimension" in _codes(translated)
    assert "model_height" in _codes(translated)
    assert any(
        item.code == "model_body_dimension" and "bottom Z" in item.message
        for item in translated.findings
    )
    assert any(item.code == "model_height" for item in translated.findings)

    spec = _vqfn_spec()
    height = spec.package.height
    height_nominal = height.nom
    assert height_nominal is not None
    package = spec.package.model_copy(
        update={
            "height": Dimension(
                min=height_nominal - 0.05,
                nom=height_nominal,
                max=height_nominal + 0.02,
                reading=height.reading,
            )
        }
    )
    bounded_spec = spec.model_copy(update={"package": package})

    def add_overheight_solid(shape: occt.Shape) -> occt.Shape:
        return occt.compound(
            (
                *occt.solids(shape),
                occt.box(
                    -0.01,
                    -0.01,
                    height_nominal + 0.01,
                    0.02,
                    0.02,
                    0.05,
                ),
            )
        )

    overheight, _ = _rewrite_model(
        tmp_path / "overheight",
        monkeypatch,
        spec=bounded_spec,
        mutation=add_overheight_solid,
    )
    assert "model_height" in _codes(overheight)


def test_model_geometry_requires_pin_marker_and_separate_terminals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_marker(shape: occt.Shape) -> occt.Shape:
        parts, body_index = _model_body_and_terminals(shape)
        body_bounds = occt.inspect(parts[body_index]).solids[0].bbox
        parts[body_index] = occt.box(
            body_bounds.x_min,
            body_bounds.y_min,
            body_bounds.z_min,
            body_bounds.x_max - body_bounds.x_min,
            body_bounds.y_max - body_bounds.y_min,
            body_bounds.z_max - body_bounds.z_min,
        )
        return occt.compound(tuple(parts))

    marker, _ = _rewrite_model(
        tmp_path / "no-marker",
        monkeypatch,
        mutation=remove_marker,
    )
    assert "model_pin1_unverifiable" in _codes(marker)

    def fuse_terminals(shape: occt.Shape) -> occt.Shape:
        parts, body_index = _model_body_and_terminals(shape)
        bounds = [
            occt.inspect(part).solids[0].bbox
            for index, part in enumerate(parts)
            if index != body_index
        ]
        fused_bounds = (
            min(bounds, key=lambda item: item.x_min).x_min,
            min(bounds, key=lambda item: item.y_min).y_min,
            min(bounds, key=lambda item: item.z_min).z_min,
            max(bounds, key=lambda item: item.x_max).x_max,
            max(bounds, key=lambda item: item.y_max).y_max,
            max(bounds, key=lambda item: item.z_max).z_max,
        )
        terminal_block = occt.box(
            fused_bounds[0],
            fused_bounds[1],
            fused_bounds[2],
            fused_bounds[3] - fused_bounds[0],
            fused_bounds[4] - fused_bounds[1],
            fused_bounds[5] - fused_bounds[2],
        )
        return occt.compound((parts[body_index], terminal_block))

    fused, _ = _rewrite_model(
        tmp_path / "fused",
        monkeypatch,
        spec=_vqfn_spec(),
        mutation=fuse_terminals,
    )
    assert "model_terminals_unseparable" in _codes(fused)


def test_model_geometry_rejects_nonidentity_transform_and_wrong_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transformed, _ = _rewrite_model(
        tmp_path / "transformed",
        monkeypatch,
        model_update={"offset": (0.1, 0.0, 0.0)},
    )
    assert "model_transform_not_identity" in _codes(transformed)

    wrong_format, _ = _rewrite_model(
        tmp_path / "wrong-format",
        monkeypatch,
        model_update={"path": "fixture.wrl"},
    )
    assert "model_format" in _codes(wrong_format)


def test_single_fused_model_recovers_body_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _ = _rewrite_model(
        tmp_path,
        monkeypatch,
        mutation=lambda shape: occt.fuse(occt.solids(shape)),
    )
    assert "model_body_dimension" not in _codes(report)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("mirror_x", "footprint_chirality_mismatch"),
        ("rotate_180", "footprint_rotation_mismatch"),
        ("swap_order", "footprint_order_mismatch"),
    ],
)
def test_vqfn_pinout_orientation_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str,
) -> None:
    report, _ = _vqfn_case(
        tmp_path / mutation,
        monkeypatch,
        mutation=mutation,
    )
    assert expected_code in _codes(report)


def test_dual_pinout_mirrored_footprint_reports_chirality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _ = _verify(
        tmp_path,
        monkeypatch,
        pad_transform=lambda pad: (
            pad.number,
            -pad.x,
            pad.y,
            pad.width,
            pad.height,
            0.0,
        ),
    )

    assert "footprint_chirality_mismatch" in _codes(report)


def test_symbol_pinout_mismatch_includes_permutation_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _vqfn_spec()
    shifted_names = {str(number): spec.pins[number % 16].name for number in range(1, 17)}
    report, _ = _vqfn_case(
        tmp_path / "symbol-shift",
        monkeypatch,
        symbol_kwargs={"name_overrides": shifted_names},
    )

    assert "symbol_pinout_name_mismatch" in _codes(report)
    assert "symbol_permutation_diagnosis" in _codes(report)
