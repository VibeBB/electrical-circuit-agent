from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from PIL import Image

from circuit import datasheet, kicad_cli, occt, pinout, sexpr
from circuit.datasheet import PdfWord
from circuit.libtestboard import write_model_export_board
from circuit.modeloracle import verify_model_export
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinCorner,
    PinSpec,
    PinTable,
    Reading,
    SpecFinding,
    _pin1_corner_findings,  # pyright: ignore[reportPrivateUsage]
    parse_dimension_text,
)
from circuit.ruleprofile import load_rules
from circuit.visionread import (
    _padded_bbox,  # pyright: ignore[reportPrivateUsage]
    _render_pdfium,  # pyright: ignore[reportPrivateUsage]
)


def _reading(text: str) -> Reading:
    return Reading(
        page=1,
        bbox=(0, 0, 1, 1),
        vision=text,
        vision_record="metamorphic-fixture",
    )


def _dimension_pdf(path: Path) -> Path:
    stream = b"BT /F1 10 Tf 20 160 Td (3.0) Tj ET\n"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        4: f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id, body in objects.items():
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for object_id in range(1, 6):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(pdf)
    return path


def _package(
    *,
    view: Literal["top", "bottom"] = "top",
    corner: PinCorner = "top_left",
) -> PackageSpec:
    return PackageSpec(
        family="no_lead_quad",
        drawing_id="Metamorphic fixture",
        pin_count=4,
        body_length=Dimension(nom=2, reading=_reading("2")),
        body_width=Dimension(nom=2, reading=_reading("2")),
        height=Dimension(nom=0.8, reading=_reading("0.8")),
        pins_per_side=(1, 1, 1, 1),
        drawing_view=view,
        pin1_corner=corner,
        pin1_reading=_reading("pin 1 at top left"),
    )


def _rotate_point(point: tuple[float, float], turns: int) -> tuple[float, float]:
    x, y = point
    for _ in range(turns):
        x, y = -y, x
    return x, y


def _rotate_corner(corner: str, turns: int) -> str:
    corners = ["top_left", "top_right", "bottom_right", "bottom_left"]
    return corners[(corners.index(corner) + turns) % len(corners)]


def test_raster_dpi_preserves_crop_points_and_mechanical_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = _dimension_pdf(tmp_path / "dimension.pdf")

    def rasterize(
        _source: Path,
        out_dir: Path,
        *,
        dpi: int,
        first_page: int | None = None,
        last_page: int | None = None,
    ) -> list[Path]:
        assert first_page == last_page == 1
        image_path = out_dir / "page-1.png"
        Image.new("RGB", (dpi, dpi), "white").save(image_path, format="PNG")
        return [image_path]

    def poppler_words(_pdf_path: Path, _page_number: int) -> tuple[list[PdfWord], str]:
        return [], ""

    def tool_version(_command: list[str], *, stderr: bool = False) -> str:
        return "test"

    monkeypatch.setattr(datasheet, "rasterize", rasterize)
    monkeypatch.setattr(datasheet, "_poppler_words", poppler_words)
    monkeypatch.setattr(datasheet, "_tool_version", tool_version)

    crop_bbox: tuple[float, float, float, float] | None = None
    readings: list[tuple[list[str], str | None, float | None, float | None]] = []
    page_size: tuple[float, float] | None = None
    for dpi in (300, 600, 1200):
        extraction_dir = tmp_path / f"extraction-{dpi}"
        extraction = datasheet.extract_datasheet(pdf_path, extraction_dir, dpi=dpi)
        page = extraction.pages[0]
        assert page.dpi == dpi
        current_size = (page.width_pt, page.height_pt)
        current_crop = _padded_bbox((15, 20, 80, 65), *current_size)
        if page_size is None:
            page_size, crop_bbox = current_size, current_crop
        assert current_size == page_size
        assert current_crop == crop_bbox

        words = datasheet.page_words(extraction, extraction_dir, 1, lanes=("pdfplumber",))
        parsed = parse_dimension_text(" ".join(word.text for word in words))
        readings.append((parsed.numbers, parsed.kind, parsed.nom, parsed.max))

        crop_path = tmp_path / f"crop-{dpi}.png"
        _render_pdfium(
            pdf_path,
            crop_path,
            1,
            current_crop,
            page.width_pt,
            page.height_pt,
            dpi,
        )
        with Image.open(crop_path) as crop:
            expected_width = (current_crop[2] - current_crop[0]) * dpi / 72
            expected_height = (current_crop[3] - current_crop[1]) * dpi / 72
            assert abs(crop.width - expected_width) <= 1
            assert abs(crop.height - expected_height) <= 1

    assert readings == [readings[0]] * 3
    assert readings[0][0] == ["3"]


def test_joint_quarter_turn_preserves_pin_orientation_verdict() -> None:
    positions = {
        "1": (-1.0, -1.0),
        "2": (1.0, -1.0),
        "3": (1.0, 1.0),
        "4": (-1.0, 1.0),
    }
    baseline = pinout.compare_orientation(positions, positions)
    assert baseline == []
    package = _package()
    for turns in range(4):
        rotated = {number: _rotate_point(point, turns) for number, point in positions.items()}
        rotated_package = package.model_copy(
            update={"pin1_corner": _rotate_corner(package.pin1_corner, turns)}
        )
        findings: list[SpecFinding] = []
        _pin1_corner_findings(
            rotated_package,
            _reading(f"pin 1 at {rotated_package.pin1_corner.replace('_', ' ')}"),
            "package.pin1_corner",
            findings,
        )
        assert findings == []
        assert pinout.compare_orientation(rotated, rotated) == baseline


def test_mirrored_bottom_drawing_preserves_orientation_verdict() -> None:
    top_positions = {
        "1": (-1.0, -1.0),
        "2": (1.0, -1.0),
        "3": (1.0, 1.0),
        "4": (-1.0, 1.0),
    }
    footprint_positions = dict(top_positions)
    baseline = pinout.compare_orientation(top_positions, footprint_positions)

    bottom_drawing = {number: (-x, y) for number, (x, y) in top_positions.items()}
    package = _package(view="bottom")
    normalized_drawing = pinout.to_top_view(bottom_drawing, package.drawing_view)
    assert normalized_drawing == top_positions
    assert pinout.compare_orientation(normalized_drawing, footprint_positions) == baseline

    findings: list[SpecFinding] = []
    _pin1_corner_findings(
        package,
        _reading("pin 1 at top right"),
        "package.pin1_corner",
        findings,
    )
    assert findings == []


def test_partspec_unit_conversion_is_skipped_without_unit_support() -> None:
    if all("unit" not in model.model_fields for model in (PartSpec, PackageSpec, Dimension)):
        pytest.skip("PartSpec dimensions are documented in mm and expose no unit field")
    pytest.fail("Add a mm/in conversion metamorphic test when PartSpec gains unit support")


def _export_spec() -> PartSpec:
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="METAMORPHIC",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=PackageSpec(
            family="chip",
            drawing_id="Fixture",
            pin_count=2,
            body_length=Dimension(nom=1.4, reading=_reading("1.4")),
            body_width=Dimension(nom=0.8, reading=_reading("0.8")),
            height=Dimension(nom=1, reading=_reading("1")),
            lead_length=Dimension(nom=0.6, reading=_reading("0.6")),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=_reading("pin 1 at top left"),
        ),
        pins=[
            PinSpec(number="1", name="VIN", electrical_type="input", reading=_reading("VIN")),
            PinSpec(number="2", name="GND", electrical_type="power_in", reading=_reading("GND")),
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="METAMORPHIC",
                package_designator="Fixture",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("METAMORPHIC Fixture"),
            )
        ],
    )


def _export_model() -> occt.Shape:
    return occt.compound(
        [
            occt.box(-0.7, -0.4, 0.2, 1.4, 0.8, 0.8),
            occt.box(-1.3, -0.4, 0, 0.6, 0.8, 0.2),
            occt.box(0.7, -0.4, 0, 0.6, 0.8, 0.2),
        ]
    )


def _export_footprint(model_path: Path) -> str:
    return f"""(footprint "FixtureFootprint" (layer "F.Cu")
  (attr smd)
  (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
    (effects (font (size 1 1))))
  (property "Value" "FixtureFootprint" (at 0 2 0) (layer "F.Fab")
    (effects (font (size 1 1))))
  (fp_rect (start -2 -1) (end 2 1)
    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
  (pad "1" smd rect (at -1 0) (size 0.8 1)
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1 0) (size 0.8 1)
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "{model_path}"
    (offset (xyz 0 0 0))
    (scale (xyz 1 1 1))
    (rotate (xyz 0 0 0))))
"""


@pytest.mark.parametrize("placement_xy_mm", [(0.0, 0.0), (23.5, -17.25)])
def test_export_oracle_is_invariant_under_board_offset_and_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    placement_xy_mm: tuple[float, float],
) -> None:
    model_path = tmp_path / "Fixture.step"
    occt.write_step(_export_model(), model_path, product_name="Fixture")
    footprint_path = tmp_path / "Fixture.kicad_mod"
    footprint_path.write_text(_export_footprint(model_path), encoding="utf-8")
    rotations = iter((0.0, 90.0))

    def run(args: list[str], **_kwargs: object) -> kicad_cli.CompletedRun:
        output = Path(args[args.index("--output") + 1])
        rotation = next(rotations)
        exported = occt.transform(
            occt.read_step(model_path),
            translation=(*placement_xy_mm, 1.595),
            rotation_z_deg=rotation,
        )
        occt.write_step(exported, output, product_name="KiCad export")
        return kicad_cli.CompletedRun(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kicad_cli, "run", run)
    report = verify_model_export(
        _export_spec(),
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
        placement_xy_mm=placement_xy_mm,
    )

    assert report.verdict == "pass", {
        "findings": [(finding.code, finding.message) for finding in report.findings],
        "runs": [
            (
                run.rotation_deg,
                run.expected_terminal_count,
                run.exported_terminal_count,
                run.terminal_centers_xy,
            )
            for run in report.runs
        ],
    }
    assert [run.rotation_deg for run in report.runs] == [0.0, 90.0]
    assert all(run.passed and run.expected_terminal_count == 2 for run in report.runs)
    assert all(
        _first_footprint_at(
            tmp_path / "oracle" / f"rotation-{int(run.rotation_deg)}" / "test-board.kicad_pcb"
        )
        == (*placement_xy_mm, run.rotation_deg)
        for run in report.runs
    )


def _first_footprint_at(board_path: Path) -> tuple[float, float, float]:
    root = sexpr.parse_text(board_path.read_text(encoding="utf-8"))
    footprint = next(
        child for child in root[1:] if isinstance(child, list) and child and child[0] == "footprint"
    )
    at = next(
        child for child in footprint[1:] if isinstance(child, list) and child and child[0] == "at"
    )
    return float(str(at[1])), float(str(at[2])), float(str(at[3]))


def test_model_export_board_writes_requested_placement(tmp_path: Path) -> None:
    model_path = tmp_path / "Fixture.step"
    occt.write_step(_export_model(), model_path, product_name="Fixture")
    footprint_path = tmp_path / "Fixture.kicad_mod"
    footprint_path.write_text(_export_footprint(model_path), encoding="utf-8")
    placement = (4.25, -8.5)
    board_path = write_model_export_board(
        tmp_path / "board",
        spec=_export_spec(),
        footprint_path=footprint_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        rotation_deg=90,
        placement_xy_mm=placement,
        model_reference_override=str(model_path),
    )
    assert _first_footprint_at(board_path) == (*placement, 90.0)
