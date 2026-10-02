"""Build hash-bound visual comparisons for library footprints and symbols."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Literal

import pdfplumber
from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import datasheet, kicad_cli, libreview, libtestboard, raster, ruleprofile, visionread
from .landpattern import Density, compute_land_pattern
from .libitems import parse_footprint
from .partspec import Dimension, PartSpec, load_part_spec, part_spec_sha256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _union(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    if not boxes:
        raise ValueError("datasheet comparison region has no cited drawing bounds")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _dimension_region(
    dimensions: list[Dimension],
) -> tuple[int, tuple[float, float, float, float]]:
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for dimension in dimensions:
        bbox = dimension.reading.bbox
        page = dimension.reading.page
        if bbox is not None and page is not None and dimension.reading.alternative_evidence is None:
            by_page.setdefault(page, []).append(bbox)
    if not by_page:
        raise ValueError("datasheet comparison region has no cited dimension bounds")
    page = max(by_page, key=lambda number: (len(by_page[number]), -number))
    return page, _union(by_page[page])


def _comparison_region(
    spec: PartSpec,
    kind: Literal["compare_footprint", "compare_symbol", "compare_model"],
) -> tuple[int, tuple[float, float, float, float]]:
    if kind == "compare_symbol":
        if spec.pinout is None:
            raise ValueError("PartSpec has no pinout drawing for symbol comparison")
        if spec.pinout.view_reading.alternative_evidence is not None:
            raise ValueError("pinout comparison requires a datasheet-backed view reading")
        return spec.pinout.page, spec.pinout.bbox
    if kind == "compare_model":
        reading = spec.package.pin1_reading
        bbox = reading.bbox
        if bbox is None or reading.page is None or reading.alternative_evidence is not None:
            raise ValueError("package pin-1 drawing region is missing")
        x0, y0, x1, y1 = bbox
        margin = 36.0
        return reading.page, (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    if spec.land_pattern is not None and spec.land_pattern.dimensions:
        return _dimension_region(list(spec.land_pattern.dimensions.values()))
    dimensions = [spec.package.body_length, spec.package.body_width]
    if spec.package.pitch is not None:
        dimensions.append(spec.package.pitch)
    page, dimension_bbox = _dimension_region(dimensions)
    pin1 = spec.package.pin1_reading
    if pin1.page == page and pin1.bbox is not None and pin1.alternative_evidence is None:
        return page, _union([dimension_bbox, pin1.bbox])
    return page, dimension_bbox


def _select_svg(exported: list[Path], expected_stem: str) -> Path:
    selected = next(
        (
            path
            for path in exported
            if path.suffix.casefold() == ".svg" and path.stem == expected_stem
        ),
        None,
    )
    if selected is None:
        raise ValueError(f"KiCad SVG export did not produce {expected_stem}.svg")
    return selected


def _composite(
    left_path: Path,
    right_path: Path,
    output: Path,
    *,
    left_label: str = "DATASHEET",
    right_label: str = "KICAD LIBRARY",
) -> tuple[int, int]:
    with Image.open(left_path) as opened:
        left = opened.convert("RGB")
    with Image.open(right_path) as opened:
        right = opened.convert("RGB")
    gutter = max(24, round(min(left.height, right.height) * 0.025))
    header_height = max(32, round(min(left.height, right.height) * 0.04))
    height = header_height + max(left.height, right.height)
    width = left.width + gutter + right.width
    if max(height, width) > 12000:
        raise ValueError("comparison render is too large")
    image = Image.new("RGB", (width, height), "white")
    image.paste(left, (0, header_height + (height - header_height - left.height) // 2))
    image.paste(
        right,
        (left.width + gutter, header_height + (height - header_height - right.height) // 2),
    )
    split_x = left.width + gutter // 2
    draw = ImageDraw.Draw(image)
    draw.line((split_x, header_height, split_x, height), fill=(120, 120, 120), width=2)
    font = ImageFont.load_default(size=max(16, min(28, header_height - 4)))
    draw.text((8, 8), left_label, fill=(32, 32, 32), font=font)
    draw.text((left.width + gutter + 8, 8), right_label, fill=(32, 32, 32), font=font)
    image.save(output, format="PNG")
    return split_x, left.height


def compare_library_item(
    spec_path: Path,
    *,
    kind: Literal["compare_footprint", "compare_symbol"],
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    density: Density = "nominal",
    out_dir: Path | None = None,
    lane: str = "main",
    profile: str = "",
    model: str = "unknown",
) -> visionread.VisionBatch:
    spec = load_part_spec(spec_path)
    spec_dir = spec_path.resolve().parent
    extraction_path = Path(spec.datasheet.extraction_path)
    if not extraction_path.is_absolute():
        extraction_path = spec_dir / extraction_path
    page_number, bbox = _comparison_region(spec, kind)
    artifact_kind: Literal["footprint", "symbol"] = (
        "footprint" if kind == "compare_footprint" else "symbol"
    )
    artifact_path = footprint_path if artifact_kind == "footprint" else symbol_lib
    artifact_sha256 = _sha256(artifact_path)
    spec_sha256 = part_spec_sha256(spec_path)
    with tempfile.TemporaryDirectory(prefix="circuit-vision-compare-") as temporary:
        temporary_dir = Path(temporary)
        crop_path = temporary_dir / "datasheet.png"
        crop_bbox, dpi, rasterizer = visionread.render_datasheet_crop(
            extraction_path,
            page_number,
            bbox,
            crop_path,
            lane=lane,
        )
        right_png = temporary_dir / "library.png"
        geometry: libreview._OverlayGeometry | None = None  # pyright: ignore[reportPrivateUsage]
        if artifact_kind == "footprint":
            footprint = parse_footprint(footprint_path)
            extraction = datasheet.load_extraction(extraction_path)
            pdf_path = Path(extraction.pdf_path)
            if not pdf_path.is_absolute():
                pdf_path = extraction_path.resolve().parent / pdf_path
            reference = compute_land_pattern(spec, density)
            crop_record = libreview._CropRecord(  # pyright: ignore[reportPrivateUsage]
                field="compare_footprint",
                page=page_number,
                path=crop_path.as_posix(),
                sha256=_sha256(crop_path),
                source_png_sha256="",
                bbox=bbox,
                crop_bbox=crop_bbox,
                scale=1.0,
            )
            with pdfplumber.open(pdf_path) as document:
                if not 1 <= page_number <= len(document.pages):
                    raise ValueError("comparison page is missing from the datasheet PDF")
                geometry = libreview._derive_overlay_geometry(  # pyright: ignore[reportPrivateUsage]
                    document.pages[page_number - 1],
                    crop_record,
                    spec,
                    footprint,
                    reference,
                    dpi=dpi,
                )
            if not geometry.scale_known or geometry.scale_px_per_mm is None:
                raise ValueError("land-pattern drawing scale could not be established")
            exported = kicad_cli.export(
                "fp_svg",
                footprint_path.parent,
                temporary_dir / "footprint-svg",
            )
            svg_path = _select_svg(exported, footprint_path.stem)
            svg_dpi = max(72, round(geometry.scale_px_per_mm * 25.4))
            rendered = raster.rasterize(svg_path, temporary_dir / "footprint-png", dpi=svg_dpi)
            if len(rendered) != 1:
                raise ValueError("KiCad footprint SVG did not render to one PNG")
            right_png = rendered[0]
        else:
            exported = kicad_cli.export(
                "sym_svg",
                symbol_lib,
                temporary_dir / "symbol-svg",
                symbol_name=symbol_name,
            )
            svg_path = _select_svg(exported, symbol_name)
            rendered = raster.rasterize(svg_path, temporary_dir / "symbol-png", dpi=300)
            if len(rendered) != 1:
                raise ValueError("KiCad symbol SVG did not render to one PNG")
            right_png = rendered[0]
            with Image.open(crop_path) as opened:
                target_height = opened.height
            with Image.open(right_png) as opened:
                right_image = opened.convert("RGB")
            if right_image.height != target_height:
                width = max(1, round(right_image.width * target_height / right_image.height))
                right_image = right_image.resize(  # pyright: ignore[reportUnknownMemberType]
                    (width, target_height),
                    resample=Image.Resampling.LANCZOS,
                )
                right_image.save(right_png, format="PNG")
        composite_path = temporary_dir / "composite.png"
        split_x, _ = _composite(crop_path, right_png, composite_path)
        return visionread.create_comparison_batch(
            extraction_path,
            composite_path,
            kind=kind,
            page=page_number,
            bbox=bbox,
            crop_bbox=crop_bbox,
            dpi=dpi,
            rasterizer=rasterizer,
            split_x=split_x,
            spec_sha256=spec_sha256,
            artifact_sha256=artifact_sha256,
            artifact_kind=artifact_kind,
            out_dir=out_dir,
            lane=lane,
            profile=profile,
            model=model,
        )


def _model_board_extents(footprint_path: Path) -> tuple[float, float]:
    footprint = parse_footprint(footprint_path)
    points = [
        point
        for graphic in footprint.graphics
        if graphic.layer in {"F.CrtYd", "B.CrtYd"}
        for point in graphic.points
    ]
    if points:
        left = min(point[0] for point in points)
        top = min(point[1] for point in points)
        right = max(point[0] for point in points)
        bottom = max(point[1] for point in points)
    else:
        copper_pads = [
            pad for pad in footprint.pads if any(layer.endswith(".Cu") for layer in pad.layers)
        ]
        if not copper_pads:
            raise ValueError("footprint has no courtyard or copper pads for model rendering")
        left = min(pad.x - pad.width / 2 for pad in copper_pads)
        top = min(pad.y - pad.height / 2 for pad in copper_pads)
        right = max(pad.x + pad.width / 2 for pad in copper_pads)
        bottom = max(pad.y + pad.height / 2 for pad in copper_pads)
    return right - left + 2.0, bottom - top + 2.0


def _scale_model_render(
    source: Path,
    destination: Path,
    *,
    board_width_mm: float,
    board_height_mm: float,
    target_pixels_per_mm: float,
) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    background = image.getpixel((0, 0))
    mask = ImageChops.difference(image, Image.new("RGB", image.size, background)).convert("L")
    bounds = mask.getbbox()
    if bounds is None:
        raise ValueError("KiCad 3D render has no visible board")
    board_pixels_width = bounds[2] - bounds[0]
    board_pixels_height = bounds[3] - bounds[1]
    if board_pixels_width <= 0 or board_pixels_height <= 0:
        raise ValueError("KiCad 3D render has invalid board bounds")
    scale_x = board_pixels_width / board_width_mm
    scale_y = board_pixels_height / board_height_mm
    if abs(scale_x - scale_y) / max(scale_x, scale_y) > 0.05:
        raise ValueError("KiCad 3D render is not an orthographic top view")
    pixels_per_mm = (scale_x + scale_y) / 2
    target_size = (
        round(board_width_mm * target_pixels_per_mm),
        round(board_height_mm * target_pixels_per_mm),
    )
    if max(target_size) > 12000:
        raise ValueError("same-scale model render is too large")
    crop = image.crop(bounds).resize(  # pyright: ignore[reportUnknownMemberType]
        target_size,
        resample=Image.Resampling.LANCZOS,
    )
    if pixels_per_mm <= 0:
        raise ValueError("KiCad 3D render has invalid physical scale")
    destination.parent.mkdir(parents=True, exist_ok=True)
    crop.save(destination, format="PNG")


def compare_model(
    spec_path: Path,
    footprint_path: Path,
    model_path: Path,
    *,
    out_dir: Path | None = None,
    lane: str = "main",
    profile: str = "",
    model: str = "unknown",
) -> visionread.VisionBatch:
    spec = load_part_spec(spec_path)
    spec_path = spec_path.resolve(strict=True)
    footprint_path = footprint_path.resolve(strict=True)
    model_path = model_path.resolve(strict=True)
    if not footprint_path.is_file() or not model_path.is_file():
        raise ValueError("model comparison requires existing footprint and STEP model files")
    extraction_path = Path(spec.datasheet.extraction_path)
    if not extraction_path.is_absolute():
        extraction_path = spec_path.parent / extraction_path
    page_number, bbox = _comparison_region(spec, "compare_model")
    extraction = datasheet.load_extraction(extraction_path)
    page = next((item for item in extraction.pages if item.page == page_number), None)
    if page is None:
        raise ValueError(f"model comparison page {page_number} is missing")
    bbox = (
        max(0.0, bbox[0]),
        max(0.0, bbox[1]),
        min(page.width_pt, bbox[2]),
        min(page.height_pt, bbox[3]),
    )
    datasheet_view = spec.package.drawing_view
    left_mirrored = datasheet_view == "bottom"
    artifact_sha256 = _sha256(model_path)
    additional_bindings = {
        "footprint_sha256": _sha256(footprint_path),
        "model_sha256": artifact_sha256,
        "datasheet_view": datasheet_view,
        "left_mirrored": str(left_mirrored).lower(),
    }
    with tempfile.TemporaryDirectory(prefix="circuit-vision-model-") as temporary:
        temporary_dir = Path(temporary)
        crop_path = temporary_dir / "datasheet.png"
        crop_bbox, dpi, rasterizer = visionread.render_datasheet_crop(
            extraction_path,
            page_number,
            bbox,
            crop_path,
            lane=lane,
        )
        if left_mirrored:
            with Image.open(crop_path) as opened:
                opened.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(
                    crop_path,
                    format="PNG",
                )
        board_dir = temporary_dir / "board"
        board_path = libtestboard.write_model_export_board(
            board_dir,
            spec=spec,
            footprint_path=footprint_path,
            rules=ruleprofile.load_rules(
                "builtin:kicad-generator",
                spec_path.parent / "rules",
            ),
            rotation_deg=0.0,
            model_reference_override=str(model_path),
        )
        raw_render_path = temporary_dir / "model-raw.png"
        kicad_cli.render(
            board_path,
            raw_render_path,
            side="top",
            width=1600,
            height=1200,
            perspective=False,
            quality="high",
        )
        rendered_model_path = temporary_dir / "model.png"
        board_width_mm, board_height_mm = _model_board_extents(footprint_path)
        _scale_model_render(
            raw_render_path,
            rendered_model_path,
            board_width_mm=board_width_mm,
            board_height_mm=board_height_mm,
            target_pixels_per_mm=dpi / 25.4,
        )
        composite_path = temporary_dir / "composite.png"
        split_x, _ = _composite(
            crop_path,
            rendered_model_path,
            composite_path,
            left_label=(
                "DATASHEET (MIRRORED BOTTOM VIEW)" if left_mirrored else "DATASHEET TOP VIEW"
            ),
            right_label="KICAD 3D TOP RENDER",
        )
        additional_bindings["render_sha256"] = _sha256(rendered_model_path)
        return visionread.create_comparison_batch(
            extraction_path,
            composite_path,
            kind="compare_model",
            page=page_number,
            bbox=bbox,
            crop_bbox=crop_bbox,
            dpi=dpi,
            rasterizer=rasterizer,
            split_x=split_x,
            spec_sha256=part_spec_sha256(spec_path),
            artifact_sha256=artifact_sha256,
            artifact_kind="model3d",
            additional_bindings=additional_bindings,
            out_dir=out_dir,
            lane=lane,
            profile=profile,
            model=model,
        )
