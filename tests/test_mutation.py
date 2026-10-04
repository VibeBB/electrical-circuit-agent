from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from circuit import datasheet, kicad_cli, libverify, occt, packageid
from circuit import mutation as mutation_module
from circuit.advisory import build_review_record
from circuit.datasheet import load_extraction
from circuit.libitems import FootprintDef, PadDef, SymbolDef, SymPin, parse_symbol
from circuit.mutation import (
    CHECK_FAMILY,
    MUTATION_OPERATORS,
    MutationArtifacts,
    MutationError,
    MutationFinding,
    MutationFixture,
    family_for_code,
    library_mutation_fixture,
    library_verifier,
    run_mutations,
)
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
    check_part_spec,
)
from pinout_fixtures import QUAD16_NAMES, pinout_drawing, quad16_fixture
from test_datasheet import _pdf  # pyright: ignore[reportPrivateUsage]
from test_libverify import _vqfn_spec, _write_case  # pyright: ignore[reportPrivateUsage]
from test_visionread import _synthetic_pdf  # pyright: ignore[reportPrivateUsage]
from vision_fixtures import FIXTURE_IMPRESSION, attach_vision_reads

REPO_ROOT = Path(__file__).parents[1]
FINDING_MODULES = (
    "authoring",
    "corpus",
    "libverify",
    "pinsource",
    "partspec",
    "model3d",
    "ruleprofile",
    "libtestboard",
    "packageid",
)
EXPECTED_OPERATORS = {
    "symbol_adjacent_pin_swap",
    "symbol_pin_name_swap",
    "symbol_pin_number_offset",
    "symbol_reversed_pin_order",
    "footprint_mirror_x",
    "footprint_mirror_y",
    "footprint_rotate_90",
    "footprint_rotate_180",
    "footprint_rotate_270",
    "footprint_pad_shift_0_1mm",
    "footprint_pad_wrong_copper_layer",
    "footprint_pitch_scale_1_02",
    "footprint_ep_size_delta_20_percent",
    "footprint_mm_to_inch",
    "footprint_inch_to_mm",
    "footprint_removed_pad",
    "footprint_duplicated_pad_number",
    "footprint_swapped_pad_numbers",
    "connector_numbering_mirror",
    "tht_drill_shrink",
    "npth_to_pth",
    "mounting_pad_drop",
    "board_edge_offset_shift",
    "mating_axis_flip",
    "partspec_min_nom_max_column_shift",
    "partspec_drawing_view_flip",
    "partspec_pin1_corner_rotation",
    "partspec_sibling_package_mpn",
    "partspec_sibling_package_variant",
    "pad_rotation_change",
    "polygon_vertex_shift",
    "exposed_pad_drop",
    "depopulated_pin_added",
    "bga_row_swap",
    "tab_offset_shift",
    "model_mirror_x",
    "model_rotate_90",
    "model_rotate_180",
    "model_offset_0_1mm",
    "model_scale_25_4",
    "model_removed_pin1_marker",
}
EXPECTED_INTEGRITY_CODES = {
    "alternative_evidence_missing",
    "alternative_evidence_scope_exceeded",
    "alternative_evidence_stale",
    "alternative_evidence_used",
    "authoring_commit_unobserved",
    "authoring_lane_input_mismatch",
    "authoring_consensus_violated",
    "confidential_artifact_outside_store",
    "corpus_approval_binding_mismatch",
    "corpus_approval_event_invalid",
    "corpus_approval_unavailable",
    "corpus_confirmation_fields_missing",
    "corpus_confirmation_time_invalid",
    "corpus_manifest_changed_during_scoring",
    "corpus_truth_changed_during_scoring",
    "corpus_truth_unconfirmed",
    "datasheet_hash_mismatch",
    "datasheet_sha_mismatch",
    "package_identity_pdf_hash_mismatch",
    "evidence_sha_mismatch",
    "extraction_stale",
    "lineage_base",
    "lineage_evidence",
    "lineage_footprint_hash",
    "lineage_invalid",
    "lineage_stale_change",
    "lineage_unrecorded_change",
    "model_manifest_invalid",
    "parent_hash",
    "part_spec_unchecked",
    "provenance_missing",
    "provenance_sha_mismatch",
    "substitute_permission_missing",
    "substitute_permission_stale",
    "substitute_scope_exceeded",
    "vision_compare_missing",
    "vision_compare_stale",
    "vision_record_mismatch",
    "vision_record_missing",
}


def _reading(value: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=value,
        vision_record="fixture.json",
    )


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _fixture(tmp_path: Path, *, seed: int = 0) -> MutationFixture:
    package = PackageSpec(
        family="no_lead_quad",
        drawing_id="mutation-fixture",
        pin_count=4,
        pitch=_dimension(0.5),
        body_length=_dimension(3.0),
        body_width=_dimension(3.0),
        height=_dimension(0.8),
        lead_length=_dimension(0.3),
        lead_width=_dimension(0.25),
        exposed_pad=ExposedPad(
            number="5",
            length=_dimension(0.8),
            width=_dimension(0.8),
        ),
        pins_per_side=(1, 1, 1, 1),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 top left"),
    )
    spec = PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="MUTATION-FIXTURE",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number=str(index),
                name=f"PIN{index}",
                electrical_type="passive",
                reading=_reading(f"{index} PIN{index}"),
            )
            for index in range(1, 5)
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="MUTATION-FIXTURE",
                package_designator="QFN",
                pin_count=4,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("MUTATION-FIXTURE QFN"),
            )
        ],
    )
    pads = [
        PadDef(
            number=str(index),
            type="smd",
            shape="rect",
            x=x,
            y=y,
            rotation=0,
            width=0.5,
            height=0.3,
            drill=None,
            layers=["F.Cu", "F.Mask", "F.Paste"],
        )
        for index, (x, y) in enumerate(
            ((-1.3, -0.5), (0.5, -1.3), (1.3, 0.5), (-0.5, 1.3)),
            start=1,
        )
    ]
    pads.append(
        PadDef(
            number="5",
            type="smd",
            shape="rect",
            x=0,
            y=0,
            rotation=0,
            width=0.8,
            height=0.8,
            drill=None,
            layers=["F.Cu", "F.Mask", "F.Paste"],
        )
    )
    footprint = FootprintDef(
        name="mutation-fixture",
        attributes=["smd"],
        pads=pads,
        graphics=[],
        models=[],
        properties={},
    )
    symbol = SymbolDef(
        name="MUTATION-FIXTURE",
        pins=[
            SymPin(
                number=str(index),
                name=f"PIN{index}",
                electrical_type="passive",
                x=float(index),
                y=0.0,
                length=2.54,
                orientation=0.0,
                unit=1,
            )
            for index in range(1, 5)
        ],
        properties={},
    )
    body = occt.cylinder_cut(
        occt.box(-1.0, -1.0, 0.2, 2.0, 2.0, 0.6),
        x=-0.55,
        y=-0.55,
        top_z=0.8,
        radius=0.2,
        depth=0.05,
    )
    terminals = (
        occt.box(-1.4, -0.15, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.15, -1.4, 0.0, 0.3, 0.3, 0.2),
        occt.box(1.1, 0.35, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.65, 1.1, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.4, -0.4, 0.0, 0.8, 0.8, 0.2),
    )
    model = occt.compound((body, *terminals))
    model_path = tmp_path / "generated.step"
    occt.write_step(model, model_path, product_name=spec.mpn)
    artifacts = MutationArtifacts(
        spec=spec,
        symbol=symbol,
        footprint=footprint,
        model=occt.read_step(model_path),
        model_path=model_path,
    )

    def verify(mutated: MutationArtifacts) -> list[MutationFinding]:
        findings: list[MutationFinding] = []
        if mutated.model_path != artifacts.model_path:
            assert mutated.model_path.is_file()
            assert mutated.model_path.suffix == ".step"
            assert mutated.footprint == artifacts.footprint
            exported_volume = sum(
                solid.volume for solid in occt.inspect(occt.read_step(mutated.model_path)).solids
            )
            mutated_volume = sum(solid.volume for solid in occt.inspect(mutated.model).solids)
            assert exported_volume == pytest.approx(mutated_volume, rel=1e-8)
        elif mutated.footprint != artifacts.footprint:
            assert mutated.model_path == artifacts.model_path
            assert mutated.spec == artifacts.spec
        if mutated.symbol != artifacts.symbol:
            findings.extend(
                (
                    MutationFinding(code="symbol_pin_set", severity="error"),
                    MutationFinding(code="symbol_permutation_diagnosis", severity="error"),
                )
            )
        if mutated.footprint != artifacts.footprint:
            findings.extend(
                (
                    MutationFinding(code="pad_geometry", severity="error"),
                    MutationFinding(code="pin_pad_mapping", severity="error"),
                )
            )
        if mutated.spec != artifacts.spec:
            findings.extend(
                (
                    MutationFinding(code="mechanical_mismatch", severity="error"),
                    MutationFinding(code="pin1_location", severity="error"),
                )
            )
        if mutated.model is not artifacts.model:
            findings.extend(
                (
                    MutationFinding(code="model_body_dimension", severity="error"),
                    MutationFinding(code="model_export_mismatch", severity="error"),
                )
            )
        return findings

    return MutationFixture(artifacts=artifacts, verify=verify, seed=seed)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _table_commands(
    rows: list[list[str]],
    x_edges: tuple[int, ...],
    top: int,
    row_height: int,
    font_size: int = 8,
    header_font_size: int | None = None,
    cell_font_sizes: dict[str, int] | None = None,
) -> list[str]:
    bottom = top - row_height * len(rows)
    commands = [
        *(f"{x} {bottom} m {x} {top} l S" for x in x_edges),
        *(
            f"{x_edges[0]} {top - row_height * index} m "
            f"{x_edges[-1]} {top - row_height * index} l S"
            for index in range(len(rows) + 1)
        ),
    ]
    for row_index, row in enumerate(rows):
        row_font_size = (
            header_font_size if row_index == 0 and header_font_size is not None else font_size
        )
        for column, cell in enumerate(row):
            if cell:
                cell_font_size = (cell_font_sizes or {}).get(cell, row_font_size)
                baseline = (
                    top
                    - row_height * row_index
                    - row_height // 2
                    - max(1, (cell_font_size - 2) // 2)
                )
                lines = cell.splitlines()
                for line_index, line in enumerate(lines):
                    line_baseline = baseline + ((len(lines) - 1) / 2 - line_index) * (
                        cell_font_size + 1
                    )
                    commands.append(
                        f"BT /F1 {cell_font_size} Tf {x_edges[column] + 4} "
                        f"{line_baseline:.1f} Td ({_pdf_escape(line)}) Tj ET"
                    )
    return commands


def _synthetic_datasheet_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_pins_column: bool = True,
    ambiguous_designator: bool = False,
) -> tuple[PartSpec, Path]:
    spec = _vqfn_spec().model_copy(update={"mpn": "TESTVQFN16"})
    pin_rows = [["Pin", "Function"], *[[number, name] for number, name in QUAD16_NAMES.items()]]
    pin_rows.append(["", "EP"])
    dimension_rows = [
        ["DIMENSION", "MIN mm", "NOM mm", "MAX mm"],
        ["Pitch", "0.495", "0.5", "0.505"],
        ["Body length", "2.9", "3.0", "3.1"],
        ["Body width", "2.9", "3.0", "3.1"],
        ["Height", "0.75", "0.8", "0.85"],
        ["Lead length", "0.35", "0.4", "0.45"],
        ["Lead width", "0.18", "0.24", "0.3"],
        ["EP length", "1.61", "1.68", "1.75"],
        ["EP width", "1.61", "1.68", "1.75"],
        ["Drawing ID", "VQFN-16-1EP", "", ""],
        ["Drawing revision", "A", "", ""],
    ]
    orderable_rows = (
        [
            [
                "Orderable\nDevice",
                "Status",
                "Package\nType",
                "Package\nDrawing",
                "Pins",
                "Package\nQty",
            ],
            [spec.mpn, "ACTIVE", "VQFN", "RGT", str(spec.package.pin_count), "3000"],
            ["TESTSOIC8", "ACTIVE", "SOIC", "SOIC", "8", "3000"],
        ]
        if include_pins_column
        else [
            [
                "Orderable\nDevice",
                "Status",
                "Package\nType",
                "Package\nDrawing",
                "Package\nQty",
            ],
            [spec.mpn, "ACTIVE", "VQFN", "RGT", "3000"],
            ["TESTSOIC8", "ACTIVE", "SOIC", "SOIC", "3000"],
        ]
    )
    if include_pins_column and ambiguous_designator:
        orderable_rows.insert(
            2,
            [spec.mpn, "ACTIVE", "SOIC", "SOIC", "8", "3000"],
        )
    commands = [
        *_table_commands(pin_rows, (20, 65, 150), 760, 15),
        *_table_commands(
            dimension_rows,
            (230, 310, 375, 410, 445),
            650,
            12,
        ),
        *_table_commands(
            orderable_rows,
            (240, 277, 299, 318, 336, 346, 365)
            if include_pins_column
            else (240, 277, 299, 318, 336, 362),
            760,
            20,
            font_size=5,
            header_font_size=3,
            cell_font_sizes={"3000": 6},
        ),
        "BT /F1 9 Tf 460 866 Td (TOP VIEW) Tj ET",
        "BT /F1 9 Tf 460 826 Td (TOP LEFT) Tj ET",
    ]
    marker_stream = _synthetic_pdf(
        tmp_path / "synthetic-datasheet.pdf",
        page_size=(1500, 1300),
        rectangle=(460, 799, 6, 6),
    )
    commands.extend(marker_stream.decode().splitlines())
    _, pin_positions, pin_names = quad16_fixture(QUAD16_NAMES)
    scale = 5.0
    pinout_center = (503.0, 575.0)
    for number, (x, y) in pin_positions.items():
        label_x = pinout_center[0] + x * scale
        label_top = pinout_center[1] + y * scale
        name_x, name_y = (
            (x + (-6 if x < 0 else 6), y) if abs(x) >= abs(y) else (x, y - 6 if y < 0 else y + 6)
        )
        name_center_x = pinout_center[0] + name_x * scale
        name_top = pinout_center[1] + name_y * scale
        for text, center_x, screen_top in (
            (number, label_x, label_top),
            (pin_names[number], name_center_x, name_top),
        ):
            text_x = center_x - len(text) * 1.5
            baseline = 1300 - screen_top - 4
            commands.append(
                f"BT /F1 {8 if text.isdigit() else 6} Tf "
                f"{text_x:.1f} {baseline:.1f} Td ({_pdf_escape(text)}) Tj ET"
            )
    pdf_path = _pdf(
        tmp_path / "synthetic-datasheet.pdf",
        [
            ([], 0),
            (["PACKAGE OUTLINE", "VQFN", "RGT0016C", "2.9 3.1 mm [0.114 0.122]"], 0),
            (
                [
                    "PACKAGE DIMENSIONS",
                    "SOIC8",
                    "[TESTVQFN16]",
                    "[TESTSOIC8]",
                    "5.0 5.2 mm",
                ],
                0,
            ),
        ],
        page_size=(1500, 1300),
        extra_commands=[commands, [], []],
    )
    extraction_dir = tmp_path / "datasheet-extraction"
    extraction = datasheet.extract_datasheet(pdf_path, extraction_dir, dpi=72)
    extraction_path = extraction_dir / "extraction.json"
    tables = datasheet.page_tables(extraction, extraction_dir, 1)

    def table_index(predicate: Callable[[list[list[str | None]]], bool]) -> int:
        for index, rows in enumerate(tables):
            if predicate(rows):
                return index
        raise AssertionError("synthetic datasheet table was not extracted")

    pin_table_index = table_index(lambda rows: len(rows) > 1 and rows[1][:2] == ["1", "SW"])
    dimension_table_index = table_index(lambda rows: any(row and row[0] == "Pitch" for row in rows))
    orderable_table_index = table_index(lambda rows: any(spec.mpn in row for row in rows))
    dimension_row_indices = {
        str(row[0]): index
        for index, row in enumerate(tables[dimension_table_index])
        if row and row[0] is not None
    }

    def bind_dimension(
        dimension: Dimension,
        label: str,
        minimum: float,
        nominal: float,
        maximum: float,
    ) -> Dimension:
        row = dimension_row_indices[label]
        reading = dimension.reading.model_copy(
            update={
                "bbox": (
                    310.0,
                    648.0 + row * 12,
                    445.0,
                    664.0 + row * 12,
                ),
                "cells": {
                    "min": CellRef(table=dimension_table_index, row=row, col=1),
                    "nom": CellRef(table=dimension_table_index, row=row, col=2),
                    "max": CellRef(table=dimension_table_index, row=row, col=3),
                },
                "vision": f"{minimum} {nominal} {maximum}",
                "vision_record": "datasheet-review.advisory.json",
            }
        )
        return dimension.model_copy(
            update={
                "label": label,
                "min": minimum,
                "nom": nominal,
                "max": maximum,
                "reading": reading,
            }
        )

    package = spec.package
    assert package.pitch is not None
    assert package.lead_length is not None
    assert package.lead_width is not None
    assert package.exposed_pad is not None
    package_updates: dict[str, object] = {
        "pitch": bind_dimension(package.pitch, "Pitch", 0.495, 0.5, 0.505),
        "body_length": bind_dimension(package.body_length, "Body length", 2.9, 3.0, 3.1),
        "body_width": bind_dimension(package.body_width, "Body width", 2.9, 3.0, 3.1),
        "height": bind_dimension(package.height, "Height", 0.75, 0.8, 0.85),
        "lead_length": bind_dimension(package.lead_length, "Lead length", 0.35, 0.4, 0.45),
        "lead_width": bind_dimension(package.lead_width, "Lead width", 0.18, 0.24, 0.3),
        "drawing_revision": "A",
        "pin1_reading": package.pin1_reading.model_copy(
            update={
                "bbox": (458.0, 460.0, 510.0, 485.0),
                "vision_record": "datasheet-review.advisory.json",
            }
        ),
    }
    exposed_pad = package.exposed_pad.model_copy(
        update={
            "length": bind_dimension(package.exposed_pad.length, "EP length", 1.61, 1.68, 1.75),
            "width": bind_dimension(package.exposed_pad.width, "EP width", 1.61, 1.68, 1.75),
        }
    )
    package_updates["exposed_pad"] = exposed_pad
    spec = spec.model_copy(update={"package": package.model_copy(update=package_updates)})
    if spec.land_pattern is not None:
        assert spec.package.pitch is not None
        pitch_reading = spec.package.pitch.reading
        land_dimensions = dict(spec.land_pattern.dimensions)
        land_dimensions["pitch"] = spec.land_pattern.dimensions["pitch"].model_copy(
            update={
                "label": "Pitch",
                "min": 0.495,
                "nom": 0.5,
                "max": 0.505,
                "reading": pitch_reading.model_copy(deep=True),
            }
        )
        spec = spec.model_copy(
            update={
                "land_pattern": spec.land_pattern.model_copy(update={"dimensions": land_dimensions})
            }
        )

    pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    spec = spec.model_copy(
        update={
            "datasheet": DatasheetRef(
                path=str(pdf_path.resolve()),
                sha256=pdf_sha256,
                revision="A",
                extraction_path=str(extraction_path.resolve()),
            ),
            "pin_table": PinTable(
                page=1,
                table=pin_table_index,
                number_col=0,
                name_col=1,
            ),
        }
    )
    pins: list[PinSpec] = []
    for row, pin in enumerate(spec.pins, start=1):
        reading = pin.reading.model_copy(
            update={
                "bbox": (
                    20.0,
                    540.0 + row * 15,
                    150.0,
                    555.0 + row * 15,
                ),
                "cells": {
                    "min": CellRef(table=pin_table_index, row=row, col=0),
                    "max": CellRef(table=pin_table_index, row=row, col=1),
                },
                "vision": f"{pin.number} {pin.name}",
                "vision_record": "datasheet-review.advisory.json",
            }
        )
        pins.append(pin.model_copy(update={"reading": reading}))
    orderable = spec.orderable[0].model_copy(
        update={
            "mpn": spec.mpn,
            "package_designator": "RGT",
            "row": CellRef(table=orderable_table_index, row=1, col=0),
            "reading": spec.orderable[0].reading.model_copy(
                update={
                    "bbox": (240.0, 540.0, 277.0, 580.0),
                    "vision": f"{spec.mpn} RGT {spec.package.pin_count}",
                    "vision_record": "datasheet-review.advisory.json",
                }
            ),
        }
    )
    pinout = pinout_drawing(
        QUAD16_NAMES,
        page=1,
        bbox=(408.0, 485.0, 600.0, 665.0),
        vision_record="datasheet-review.advisory.json",
    )
    pinout = pinout.model_copy(
        update={
            "view_reading": pinout.view_reading.model_copy(
                update={
                    "bbox": (458.0, 420.0, 510.0, 450.0),
                    "vision_record": "datasheet-review.advisory.json",
                }
            )
        }
    )
    spec = spec.model_copy(
        update={
            "pins": pins,
            "orderable": [orderable],
            "pinout": pinout,
        }
    )
    page_image = extraction_dir / extraction.pages[0].png_path
    review = build_review_record(
        page_image,
        model="synthetic-fixture",
        checklist="datasheet",
        impression=FIXTURE_IMPRESSION,
        findings=[],
        summary="Synthetic datasheet evidence fixture.",
    )
    review_path = tmp_path / "datasheet-review.advisory.json"
    review_path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    spec_path = tmp_path / "evidence-part-spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    spec = attach_vision_reads(spec, spec_path, extraction_path, monkeypatch)
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return spec, extraction_path


def test_package_identity_resolves_orderable_row_and_sibling_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(tmp_path, monkeypatch)
    pdf_path = Path(spec.datasheet.path)

    identity, findings = packageid.resolve_package_identity(pdf_path, spec.mpn)

    assert findings == []
    assert identity is not None
    assert identity.row_pages == [1]
    assert identity.designator == "RGT"
    assert identity.pin_count_candidates == [16]
    assert identity.drawing_page == 2
    assert identity.drawing_id == "RGT0016C"
    assert (2.9, 3.1) in identity.body_ranges_mm
    assert (0.114, 0.122) not in identity.body_ranges_mm
    assert packageid.sibling_package_mpn(pdf_path, spec.mpn) == "TESTSOIC8"


def test_package_identity_ignores_package_type_and_packaging_mpn_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(tmp_path, monkeypatch)

    identity, findings = packageid.resolve_package_identity(Path(spec.datasheet.path), spec.mpn)

    assert findings == []
    assert identity is not None
    assert identity.designator == "RGT"
    assert identity.drawing_id == "RGT0016C"


def test_package_identity_rejects_two_real_designator_drawings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(
        tmp_path,
        monkeypatch,
        ambiguous_designator=True,
    )

    identity, findings = packageid.resolve_package_identity(Path(spec.datasheet.path), spec.mpn)

    assert identity is None
    assert [item.code for item in findings] == ["package_identity_ambiguous"]


def test_package_identity_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        packageid.PackageIdentity.model_validate(
            {
                "mpn": "ABC123",
                "row_pages": [1],
                "designator": "RGT",
                "pin_count_candidates": [16],
                "drawing_page": 2,
                "drawing_id": "RGT0016C",
                "body_ranges_mm": [(2.9, 3.1)],
                "unexpected": True,
            }
        )


def test_package_identity_accepts_only_extra_thermal_pad_count() -> None:
    private_api: Any = packageid

    assert private_api._pin_count_matches(17, [16], thermal_pad=True)
    assert private_api._pin_count_matches(16, [16], thermal_pad=True)
    assert not private_api._pin_count_matches(16, [17], thermal_pad=True)
    assert not private_api._pin_count_matches(16, [17], thermal_pad=False)


def test_package_identity_uses_the_pins_column_not_package_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(tmp_path, monkeypatch)

    identity, findings = packageid.resolve_package_identity(Path(spec.datasheet.path), spec.mpn)

    assert findings == []
    assert identity is not None
    assert identity.pin_count_candidates == [16]


def test_package_identity_requires_a_pins_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(
        tmp_path,
        monkeypatch,
        include_pins_column=False,
    )

    identity, findings = packageid.resolve_package_identity(Path(spec.datasheet.path), spec.mpn)

    assert identity is not None
    assert identity.pin_count_candidates == []
    assert [item.code for item in findings] == ["package_identity_pin_count_unresolved"]


def test_package_identity_fails_closed_without_outline_or_on_lane_disagreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_outline = _pdf(tmp_path / "no-outline.pdf", [(["ABC123", "RGT", "16"], 0)])
    _, findings = packageid.resolve_package_identity(no_outline, "ABC123")
    assert [item.code for item in findings] == ["package_identity_drawing_unresolved"]
    _, findings = packageid.resolve_package_identity(no_outline, "MISSING")
    assert [item.code for item in findings] == ["package_identity_mpn_unresolved"]

    spec, _ = _synthetic_datasheet_pdf(tmp_path, monkeypatch)

    def empty_lane(_page: object) -> list[datasheet.PdfWord]:
        return []

    monkeypatch.setattr(datasheet, "pdfplumber_words", empty_lane)
    _, findings = packageid.resolve_package_identity(Path(spec.datasheet.path), spec.mpn)
    assert [item.code for item in findings] == ["package_identity_lane_mismatch"]


def _mock_package_identity_lanes(
    monkeypatch: pytest.MonkeyPatch,
    poppler_identity: packageid.PackageIdentity,
    plumber_identity: packageid.PackageIdentity,
) -> tuple[packageid.PackageIdentity | None, list[libverify.VerifyFinding]]:
    lane_results = [
        (poppler_identity, None, ""),
        (plumber_identity, None, ""),
    ]

    def extract_lane(
        _pdf_path: Path,
        *,
        poppler: bool,
    ) -> tuple[dict[int, list[datasheet.PdfWord]], str | None]:
        return {
            1: [
                datasheet.PdfWord(
                    text="TESTVQFN16",
                    x0=0.0,
                    top=0.0,
                    x1=10.0,
                    bottom=1.0,
                )
            ]
        }, None

    def resolve_lane(
        _pages: dict[int, list[datasheet.PdfWord]],
        _mpn: str,
    ) -> tuple[packageid.PackageIdentity | None, str | None, str]:
        return lane_results.pop(0)

    monkeypatch.setattr(packageid, "_extract_lane", extract_lane)
    monkeypatch.setattr(packageid, "_resolve_lane", resolve_lane)
    return packageid.resolve_package_identity(Path("unused.pdf"), "TESTVQFN16")


def _package_identity_with_ranges(
    body_ranges_mm: list[tuple[float, float]],
) -> packageid.PackageIdentity:
    return packageid.PackageIdentity(
        mpn="TESTVQFN16",
        row_pages=[32, 35, 37, 38],
        designator="RGT",
        pin_count_candidates=[16],
        drawing_page=40,
        drawing_id="RGT0016C",
        body_ranges_mm=body_ranges_mm,
    )


def test_package_identity_agrees_on_intersecting_body_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poppler_identity = _package_identity_with_ranges([(1.1, 3.4), (1.2, 3.5)])
    plumber_identity = poppler_identity.model_copy(
        update={"body_ranges_mm": [(1.104, 3.398), (1.0, 3.6), (1.2, 3.5)]}
    )

    identity, findings = _mock_package_identity_lanes(
        monkeypatch,
        poppler_identity,
        plumber_identity,
    )

    assert identity == poppler_identity
    assert findings == []


def test_package_identity_rejects_different_designator_between_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poppler_identity = _package_identity_with_ranges([(1.1, 3.4)])
    plumber_identity = poppler_identity.model_copy(
        update={"designator": "SOIC", "drawing_id": "SOIC8"}
    )

    identity, findings = _mock_package_identity_lanes(
        monkeypatch,
        poppler_identity,
        plumber_identity,
    )

    assert identity is None
    assert [item.code for item in findings] == ["package_identity_lane_mismatch"]


def test_package_identity_rejects_disjoint_body_ranges_between_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poppler_identity = _package_identity_with_ranges([(1.1, 3.4)])
    plumber_identity = poppler_identity.model_copy(update={"body_ranges_mm": [(2.0, 4.0)]})

    identity, findings = _mock_package_identity_lanes(
        monkeypatch,
        poppler_identity,
        plumber_identity,
    )

    assert identity is None
    assert [item.code for item in findings] == ["package_identity_lane_mismatch"]


def test_package_identity_drawing_ids_preserve_original_case_and_length(
    tmp_path: Path,
) -> None:
    lowercase_drawing = _pdf(
        tmp_path / "lowercase-drawing.pdf",
        [(["ABC123", "RGT", "16"], 0), (["PACKAGE OUTLINE", "rgt0016c"], 0)],
    )
    identity, findings = packageid.resolve_package_identity(lowercase_drawing, "ABC123")
    assert identity is None
    assert [item.code for item in findings] == ["package_identity_drawing_unresolved"]

    short_drawing = _pdf(
        tmp_path / "short-drawing.pdf",
        [(["ABC123", "RG", "16"], 0), (["PACKAGE OUTLINE", "RG1"], 0)],
    )
    identity, findings = packageid.resolve_package_identity(short_drawing, "ABC123")
    assert identity is None
    assert [item.code for item in findings] == ["package_identity_drawing_unresolved"]


def test_package_identity_reverifies_pdf_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _ = _synthetic_datasheet_pdf(tmp_path, monkeypatch)
    mismatched = spec.model_copy(
        update={"datasheet": spec.datasheet.model_copy(update={"sha256": "0" * 64})}
    )
    footprint = FootprintDef(
        name="fixture",
        attributes=[],
        pads=[],
        graphics=[],
        models=[],
        properties={},
    )

    findings = packageid.check_package_identity(
        mismatched,
        footprint,
        None,
        pdf_path=Path(spec.datasheet.path),
    )

    assert [item.code for item in findings] == ["package_identity_pdf_hash_mismatch"]


def test_package_identity_accepts_the_verified_fixture_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch)
    pdf_path = Path(fixture.artifacts.spec.datasheet.path)

    findings = packageid.check_package_identity(
        fixture.artifacts.spec,
        fixture.artifacts.footprint,
        fixture.artifacts.model,
        pdf_path=pdf_path,
    )

    assert findings == []


def _known_good_library_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: int = 0,
    run_export_oracle: bool = False,
) -> MutationFixture:
    spec, _extraction_path = _synthetic_datasheet_pdf(tmp_path, monkeypatch)
    case = _write_case(
        tmp_path,
        monkeypatch,
        spec=spec,
        stub_cli=not run_export_oracle,
        record_authoring=True,
        record_comparisons=False,
    )
    spec, _, spec_path, _, symbol_path, footprint_path = case
    exposed_pad = spec.package.exposed_pad
    assert exposed_pad is not None
    footprint_text = footprint_path.read_text(encoding="utf-8")
    footprint_text, paste_margin_count = re.subn(
        rf'(\(pad "{re.escape(exposed_pad.number)}".*?\(size [^)]+\))',
        r"\1 (solder_paste_margin -0.15)",
        footprint_text,
        count=1,
    )
    assert paste_margin_count == 1
    footprint_path.write_text(footprint_text, encoding="utf-8")
    monkeypatch.delenv("CIRCUIT_AUTHORING_LANE", raising=False)
    model_path = next((tmp_path / "models").rglob(f"{spec.package.drawing_id}.step"))
    return library_mutation_fixture(
        spec_path=spec_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        model_path=model_path,
        work_dir=tmp_path / "real-verifier",
        run_export_oracle=run_export_oracle,
        seed=seed,
    )


def _string_constant(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant):
        return None
    value: object = node.value
    return value if isinstance(value, str) else None


def _finding_codes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in ast.walk(tree):
        constant = _string_constant(node)
        if constant is not None and re.fullmatch(r"package_identity_[a-z0-9_]+", constant):
            codes.add(constant)
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        for keyword in node.keywords:
            constant = _string_constant(keyword.value)
            if keyword.arg == "code" and constant is not None:
                codes.add(constant)
        code_index = 0 if path.stem == "packageid" else 1
        constant = (
            _string_constant(node.args[code_index])
            if function_name == "_finding" and len(node.args) > code_index
            else None
        )
        if constant is not None:
            codes.add(constant)
    return codes


def test_every_static_verification_code_has_a_family() -> None:
    codes = {
        code
        for module in FINDING_MODULES
        for code in _finding_codes(REPO_ROOT / "src" / "circuit" / f"{module}.py")
    }
    assert codes - CHECK_FAMILY.keys() == set()
    assert family_for_code("testboard_drc_clearance") == "rule_profile"
    assert family_for_code("testboard_erc_pin_not_connected") == "pin_bijection"
    assert family_for_code("footprint_chirality_mismatch") == "orientation"
    assert family_for_code("footprint_order_mismatch") == "orientation"
    assert family_for_code("footprint_rotation_mismatch") == "orientation"
    assert family_for_code("pin1_mismatch") == "evidence"
    assert family_for_code("view_label_mismatch") == "evidence"
    assert family_for_code("pin_source_identity_mismatch") == "package_identity"
    assert family_for_code("F6.3") == "land_geometry"
    assert {
        code for code, family in CHECK_FAMILY.items() if family == "integrity"
    } == EXPECTED_INTEGRITY_CODES
    assert family_for_code("part_spec_unchecked") == "integrity"
    with pytest.raises(MutationError, match="unmapped verification finding code"):
        family_for_code("new_unmapped_finding")


def test_mutation_operators_use_real_verifier_and_match_expected_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=41)
    spec_path = fixture.artifacts.source_spec_path
    assert spec_path is not None
    datasheet_ref = fixture.artifacts.spec.datasheet
    assert datasheet_ref is not None
    extraction_path = Path(datasheet_ref.extraction_path)
    fresh_check = check_part_spec(
        fixture.artifacts.spec,
        load_extraction(extraction_path),
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    fresh_counting_errors = [
        (finding.code, finding.message)
        for finding in fresh_check.findings
        if finding.severity == "error"
        and family_for_code(finding.code) not in {"vision", "integrity"}
    ]
    assert fresh_counting_errors == []

    baseline_findings = list(fixture.verify(fixture.artifacts))
    baseline_counting_errors = [
        finding.code
        for finding in baseline_findings
        if finding.severity == "error"
        and family_for_code(finding.code) not in {"vision", "integrity"}
    ]
    assert baseline_counting_errors == []

    first = run_mutations(fixture)

    assert {operator.name for operator in MUTATION_OPERATORS} == EXPECTED_OPERATORS
    applicable = {
        operator.name for operator in MUTATION_OPERATORS if operator.applies(fixture.artifacts)
    }
    assert {outcome.mutation.operator for outcome in first.outcomes} == applicable
    assert first.baseline_findings == [
        "pin_source_single",
        "reading_order_divergence",
        "vision_compare_missing",
        "vision_compare_missing",
    ]
    assert first.excluded_vision_findings > 0
    assert first.export_oracle_run is False
    assert first.passed is False
    assert all(outcome.finding_codes for outcome in first.outcomes)
    assert all(outcome.mutation.critical for outcome in first.outcomes)
    expected_counting_families = {
        # This non-Docker matrix intentionally records the production verifier's
        # exact counting-family coverage without KiCad's export oracle.
        "symbol_adjacent_pin_swap": ("orientation", "pin_bijection"),
        "symbol_pin_name_swap": ("orientation", "pin_bijection"),
        "symbol_pin_number_offset": ("orientation", "pin_bijection"),
        "symbol_reversed_pin_order": ("orientation", "pin_bijection"),
        "footprint_mirror_x": ("land_geometry", "model_geometry", "orientation"),
        "footprint_mirror_y": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_90": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_180": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_270": ("land_geometry", "model_geometry", "orientation"),
        "footprint_pad_shift_0_1mm": ("land_geometry", "model_geometry"),
        "footprint_pad_wrong_copper_layer": ("land_geometry",),
        "footprint_pitch_scale_1_02": ("land_geometry", "model_geometry"),
        "footprint_ep_size_delta_20_percent": ("land_geometry",),
        "pad_rotation_change": ("land_geometry", "model_geometry"),
        "exposed_pad_drop": ("land_geometry", "model_geometry", "pin_bijection"),
        "footprint_mm_to_inch": ("land_geometry", "model_geometry", "package_identity"),
        "footprint_inch_to_mm": ("land_geometry", "model_geometry", "package_identity"),
        "footprint_removed_pad": (
            "land_geometry",
            "model_geometry",
            "orientation",
            "pin_bijection",
        ),
        "footprint_duplicated_pad_number": (
            "land_geometry",
            "model_geometry",
            "orientation",
            "pin_bijection",
        ),
        "footprint_swapped_pad_numbers": ("land_geometry", "model_geometry", "orientation"),
        "partspec_min_nom_max_column_shift": (
            "evidence",
            "land_geometry",
            "model_geometry",
        ),
        "partspec_drawing_view_flip": ("evidence", "orientation"),
        "partspec_pin1_corner_rotation": ("evidence", "model_geometry", "orientation"),
        "partspec_sibling_package_mpn": ("evidence", "package_identity"),
        "partspec_sibling_package_variant": ("evidence", "package_identity"),
        "model_mirror_x": ("model_geometry",),
        "model_rotate_90": ("model_geometry",),
        "model_rotate_180": ("model_geometry",),
        "model_offset_0_1mm": ("model_geometry",),
        "model_scale_25_4": ("model_geometry", "package_identity"),
        "model_removed_pin1_marker": ("model_geometry",),
    }
    actual_counting_families = {
        outcome.mutation.operator: tuple(
            family for family in outcome.families if family not in {"vision", "integrity"}
        )
        for outcome in first.outcomes
    }
    assert actual_counting_families == {
        name: families
        for name, families in expected_counting_families.items()
        if name in applicable
    }
    assert all(
        outcome.counting_family_count
        == len(set(outcome.families).difference({"vision", "integrity"}))
        for outcome in first.outcomes
    )
    assert {
        outcome.mutation.operator for outcome in first.outcomes if outcome.counting_family_count < 2
    } == set(first.single_oracle)
    assert {
        "partspec_min_nom_max_column_shift",
        "partspec_drawing_view_flip",
        "partspec_pin1_corner_rotation",
        "partspec_sibling_package_mpn",
        "partspec_sibling_package_variant",
    } <= {
        outcome.mutation.operator for outcome in first.outcomes if "integrity" in outcome.families
    }


def test_partspec_mutations_rederive_pdf_evidence_against_unchanged_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=32032)
    original_verify = libverify.verify_library_part
    staged_specs: list[Path] = []

    def capture_verify(*args: object, **kwargs: object) -> libverify.LibraryVerification:
        spec_path = cast(Path, kwargs["spec_path"])
        staged_specs.append(spec_path)
        staged_spec = PartSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        staged_spec.bind_source_file(spec_path)
        assert staged_spec == args[0]
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(libverify, "verify_library_part", capture_verify)
    operators = {operator.name: operator for operator in MUTATION_OPERATORS}
    for name in (
        "partspec_min_nom_max_column_shift",
        "partspec_drawing_view_flip",
        "partspec_pin1_corner_rotation",
        "partspec_sibling_package_mpn",
        "partspec_sibling_package_variant",
    ):
        mutated, details = operators[name].apply(fixture.artifacts, fixture.seed)
        assert mutated.footprint == fixture.artifacts.footprint
        assert mutated.model_path == fixture.artifacts.model_path
        if name == "partspec_sibling_package_variant":
            assert mutated.spec.mpn == "TESTSOIC8"
            assert mutated.spec.orderable[0].mpn == "TESTSOIC8"
            assert details.params["sibling_source"] == "pdf"
        findings = list(fixture.verify(mutated))
        assert any(
            finding.severity == "error" and family_for_code(finding.code) == "evidence"
            for finding in findings
        )

    assert len(staged_specs) == 5
    assert len({path.parent for path in staged_specs}) == 5


def test_partspec_mutation_rechecks_evidence_instead_of_using_stale_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=32032)
    source_spec_path = fixture.artifacts.source_spec_path
    assert source_spec_path is not None
    datasheet_ref = fixture.artifacts.spec.datasheet
    assert datasheet_ref is not None
    extraction_path = Path(datasheet_ref.extraction_path)
    check = check_part_spec(
        fixture.artifacts.spec,
        load_extraction(extraction_path),
        spec_path=source_spec_path,
        extraction_path=extraction_path,
    )
    source_check_path = source_spec_path.parent / "part.spec.check.json"
    source_check_path.write_text(check.model_dump_json(indent=2) + "\n", encoding="utf-8")
    artifacts = replace(fixture.artifacts, spec_check_path=source_check_path)
    fixture = replace(fixture, artifacts=artifacts)

    original_verify = libverify.verify_library_part
    passed_check_paths: list[Path | None] = []

    def capture_verify(*args: object, **kwargs: object) -> libverify.LibraryVerification:
        check_path = kwargs.get("spec_check_path")
        assert check_path is None or isinstance(check_path, Path)
        passed_check_paths.append(check_path)
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(libverify, "verify_library_part", capture_verify)
    baseline_findings = list(fixture.verify(artifacts))
    operator = next(
        item for item in MUTATION_OPERATORS if item.name == "partspec_drawing_view_flip"
    )
    mutated, _details = operator.apply(artifacts, fixture.seed)

    mutated_findings = list(fixture.verify(mutated))

    assert passed_check_paths[0] is not None
    assert not any(
        finding.severity == "error" and family_for_code(finding.code) == "evidence"
        for finding in baseline_findings
    )
    assert passed_check_paths[1] is None
    assert any(
        finding.severity == "error" and family_for_code(finding.code) == "evidence"
        for finding in mutated_findings
    )


def test_partspec_column_shift_supports_dimensions_without_nominal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=613)
    package = fixture.artifacts.spec.package
    lead_width = package.lead_width
    assert lead_width is not None
    package_without_nominals = package.model_copy(
        update={
            "body_length": package.body_length.model_copy(update={"nom": None}),
            "body_width": package.body_width.model_copy(update={"nom": None}),
            "lead_width": lead_width.model_copy(update={"nom": None}),
        }
    )
    spec = fixture.artifacts.spec.model_copy(update={"package": package_without_nominals})
    artifacts = replace(fixture.artifacts, spec=spec)
    operator = next(
        item for item in MUTATION_OPERATORS if item.name == "partspec_min_nom_max_column_shift"
    )

    mutated, details = operator.apply(artifacts, fixture.seed)

    field = details.params["field"]
    assert isinstance(field, str)
    selected_dimension = {
        "body_length": mutated.spec.package.body_length,
        "body_width": mutated.spec.package.body_width,
        "lead_width": mutated.spec.package.lead_width,
    }[field]
    assert mutated.spec != artifacts.spec
    assert selected_dimension is not None
    assert selected_dimension.min == selected_dimension.nom == selected_dimension.max


def test_sibling_package_variant_marks_synthetic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=32032)
    operator = next(
        item for item in MUTATION_OPERATORS if item.name == "partspec_sibling_package_variant"
    )

    def no_sibling(_path: Path, _mpn: str) -> None:
        return None

    monkeypatch.setattr(mutation_module, "sibling_package_mpn", no_sibling)

    _, details = operator.apply(fixture.artifacts, fixture.seed)

    assert details.params["sibling_source"] == "synthetic"


def test_unexercised_codes_do_not_gate_baseline_or_earn_family_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _known_good_library_fixture(tmp_path, monkeypatch, seed=40404)
    unexercised_codes = frozenset({"authoring_missing", "vision_compare_missing"})

    def verify(_artifacts: MutationArtifacts) -> list[MutationFinding]:
        return [
            MutationFinding(code="authoring_missing", severity="error"),
            MutationFinding(code="vision_compare_missing", severity="error"),
        ]

    fixture = MutationFixture(
        artifacts=baseline.artifacts,
        verify=verify,
        seed=baseline.seed,
        unexercised_codes=unexercised_codes,
    )
    monkeypatch.setattr(mutation_module, "MUTATION_OPERATORS", (MUTATION_OPERATORS[0],))

    report = run_mutations(fixture)

    assert report.baseline_findings == ["authoring_missing", "vision_compare_missing"]
    assert report.unexercised_codes == ["authoring_missing", "vision_compare_missing"]
    assert report.outcomes[0].finding_codes == []
    assert report.outcomes[0].families == []
    assert report.outcomes[0].status == "undetected"


def test_symbol_mutations_are_serialized_and_reach_real_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=41)
    original_verify = libverify.verify_library_part
    called: list[Path] = []

    def capture_verify(*args: object, **kwargs: object) -> libverify.LibraryVerification:
        called.append(cast(Path, kwargs["symbol_lib"]))
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(libverify, "verify_library_part", capture_verify)
    assert not [
        finding
        for finding in fixture.verify(fixture.artifacts)
        if finding.severity == "error"
        and family_for_code(finding.code) not in {"vision", "integrity"}
    ]

    expected = {
        "symbol_adjacent_pin_swap": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
        "symbol_pin_name_swap": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
        "symbol_reversed_pin_order": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
    }
    operators = {operator.name: operator for operator in MUTATION_OPERATORS}
    for name, expected_codes in expected.items():
        mutated, _ = operators[name].apply(fixture.artifacts, fixture.seed)
        findings = list(fixture.verify(mutated))
        codes = {finding.code for finding in findings}
        assert expected_codes <= codes
        assert {family_for_code(code) for code in expected_codes} == {
            "pin_bijection",
            "orientation",
        }
        staged_symbol = called[-1]
        parsed = parse_symbol(staged_symbol, fixture.artifacts.symbol.name)
        assert parsed == mutated.symbol

    assert len(called) == 4
    assert len({path.parent for path in called}) == len(called)


def test_symbol_mutations_reject_unchanged_number_name_maps(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, seed=9)
    numeric_indices = [
        index for index, pin in enumerate(fixture.artifacts.symbol.pins) if pin.number.isdecimal()
    ]
    assert len(numeric_indices) >= 3
    operators = {operator.name: operator for operator in MUTATION_OPERATORS}

    duplicated_number_pins = list(fixture.artifacts.symbol.pins)
    for index in numeric_indices:
        duplicated_number_pins[index] = duplicated_number_pins[index].model_copy(
            update={"number": "1", "name": f"PIN{index}"}
        )
    duplicate_number_artifacts = replace(
        fixture.artifacts,
        symbol=fixture.artifacts.symbol.model_copy(update={"pins": duplicated_number_pins}),
    )
    for name in ("symbol_adjacent_pin_swap", "symbol_pin_name_swap"):
        with pytest.raises(MutationError):
            operators[name].apply(duplicate_number_artifacts, fixture.seed)

    reversed_pins = list(fixture.artifacts.symbol.pins)
    for offset, index in enumerate(numeric_indices):
        mirror = min(offset, len(numeric_indices) - 1 - offset)
        reversed_pins[index] = reversed_pins[index].model_copy(
            update={
                "number": str(offset + 1),
                "name": "PIN_A" if mirror != 1 else "PIN_B",
            }
        )
    reversed_artifacts = replace(
        fixture.artifacts,
        symbol=fixture.artifacts.symbol.model_copy(update={"pins": reversed_pins}),
    )
    with pytest.raises(MutationError, match="number-to-name map"):
        operators["symbol_reversed_pin_order"].apply(reversed_artifacts, fixture.seed)


def test_library_verifier_fails_closed_without_kicad_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=9)
    monkeypatch.setattr(kicad_cli, "command_prefix", lambda: ["/missing/kicad-cli"])
    verifier = library_verifier(
        work_dir=tmp_path / "missing-cli",
        run_export_oracle=True,
    )

    with pytest.raises(MutationError, match="kicad-cli is unavailable"):
        verifier(fixture.artifacts)


def test_single_family_critical_mutations_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, seed=9)
    body_length = fixture.artifacts.spec.package.body_length
    assert body_length is not None
    package = fixture.artifacts.spec.package.model_copy(
        update={"body_length": body_length.model_copy(update={"min": 2.8, "max": 3.2})}
    )
    spec = fixture.artifacts.spec.model_copy(update={"package": package})
    fixture = replace(fixture, artifacts=replace(fixture.artifacts, spec=spec))

    def one_family_verifier(artifacts: MutationArtifacts) -> list[MutationFinding]:
        if artifacts == fixture.artifacts:
            return []
        return [MutationFinding(code="model_body_dimension", severity="error")]

    report = run_mutations(
        MutationFixture(
            artifacts=fixture.artifacts,
            verify=one_family_verifier,
            seed=fixture.seed,
        )
    )

    assert report.passed is False
    applicable = {
        operator.name for operator in MUTATION_OPERATORS if operator.applies(fixture.artifacts)
    }
    assert set(report.single_oracle) == applicable
    assert report.undetected == []
    assert {outcome.mutation.operator for outcome in report.outcomes} == applicable
    assert all(outcome.status == "single_oracle" for outcome in report.outcomes)


def test_mutation_operators_change_their_declared_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch)
    for operator in MUTATION_OPERATORS:
        if not operator.applies(fixture.artifacts):
            continue
        mutated, record = operator.apply(fixture.artifacts, 18)
        assert record.operator == operator.name
        assert record.target == operator.target
        assert mutated != fixture.artifacts
