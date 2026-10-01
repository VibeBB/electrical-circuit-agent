from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import pytest
from PIL import Image, ImageDraw

from circuit.datasheet import DatasheetExtraction
from circuit.partspec import (
    CellRef,
    PartSpec,
    Reading,
    _all_readings,  # pyright: ignore[reportPrivateUsage]
)
from circuit.visionread import VisionReadRequest, create_read_batch, record_answers

FIXTURE_CONTROL = "ABC234"
FIXTURE_IMPRESSION = (
    "The crop is legible and the printed marks are easy to distinguish. "
    "The surrounding geometry provides useful context without obscuring the cited value. "
    "No unexpected symbols or clipping are visible in this image. "
) * 3

_JsonObject = dict[str, object]


def _read_tables(
    extraction: DatasheetExtraction, extraction_dir: Path
) -> dict[int, list[_JsonObject]]:
    tables: dict[int, list[_JsonObject]] = {}
    for page in extraction.pages:
        if page.tables_path is None:
            continue
        path = Path(page.tables_path)
        if not path.is_absolute():
            path = extraction_dir / path
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            page_data = cast(_JsonObject, value)
            raw_tables = page_data.get("tables")
            if isinstance(raw_tables, list):
                tables[page.page] = [
                    cast(_JsonObject, table)
                    for table in cast(list[object], raw_tables)
                    if isinstance(table, dict)
                ]
    return tables


def _table(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: int,
    index: int,
) -> _JsonObject:
    tables = _read_tables(extraction, extraction_dir).get(page, [])
    if not 0 <= index < len(tables):
        raise ValueError(f"fixture table {index} is unavailable on page {page}")
    return tables[index]


def _cell_bbox(table: _JsonObject, row: int, col: int) -> tuple[float, float, float, float] | None:
    cells = table.get("cells")
    if not isinstance(cells, list):
        return None
    rows = cast(list[object], cells)
    if not 0 <= row < len(rows):
        return None
    cell_row = rows[row]
    if not isinstance(cell_row, list):
        return None
    row_cells = cast(list[object], cell_row)
    if not 0 <= col < len(row_cells):
        return None
    value = row_cells[col]
    if not isinstance(value, list):
        return None
    numbers = cast(list[object], value)
    if len(numbers) != 4 or any(not isinstance(item, (int, float)) for item in numbers):
        return None
    coordinates = cast(list[int | float], numbers)
    return (
        float(coordinates[0]),
        float(coordinates[1]),
        float(coordinates[2]),
        float(coordinates[3]),
    )


def _union(bboxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    if not bboxes:
        raise ValueError("fixture reading has no usable evidence bounds")
    return (
        min(box[0] for box in bboxes),
        min(box[1] for box in bboxes),
        max(box[2] for box in bboxes),
        max(box[3] for box in bboxes),
    )


def _reading_bbox(
    reading: Reading,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
) -> tuple[float, float, float, float]:
    if reading.bbox is not None:
        return reading.bbox
    boxes: list[tuple[float, float, float, float]] = []
    for reference in (reading.cells or {}).values():
        table = _table(extraction, extraction_dir, reading.page, reference.table)
        bbox = _cell_bbox(table, reference.row, reference.col)
        if bbox is not None:
            boxes.append(bbox)
    return _union(boxes)


def _write_render_stub(
    monkeypatch: pytest.MonkeyPatch,
    lane: Literal["a", "b"],
) -> None:
    from circuit import visionread

    def render_pdfium(
        _pdf_path: Path,
        output: Path,
        _page_number: int,
        bbox: tuple[float, float, float, float],
        _page_width: float,
        _page_height: float,
        dpi: int,
    ) -> None:
        size = (
            max(1, math.ceil((bbox[2] - bbox[0]) * dpi / 72)),
            max(1, math.ceil((bbox[3] - bbox[1]) * dpi / 72)),
        )
        Image.new("RGB", size, "white").save(output, format="PNG")

    def control_image(output: Path, size: tuple[int, int]) -> str:
        image = Image.new("RGB", size, "white")
        ImageDraw.Draw(image).text((2, 2), FIXTURE_CONTROL, fill="black")
        image.save(output, format="PNG")
        return FIXTURE_CONTROL

    if lane == "b":
        monkeypatch.setattr(visionread, "_render_pdfium", render_pdfium)
    else:

        def render_pdftoppm(
            _pdf_path: Path,
            output: Path,
            _page: int,
            bbox: tuple[float, float, float, float],
            dpi: int,
        ) -> None:
            size = (
                max(1, math.ceil((bbox[2] - bbox[0]) * dpi / 72)),
                max(1, math.ceil((bbox[3] - bbox[1]) * dpi / 72)),
            )
            Image.new("RGB", size, "white").save(output, format="PNG")

        monkeypatch.setattr(visionread, "_render_pdftoppm", render_pdftoppm)
    monkeypatch.setattr(visionread, "_control_image", control_image)


def _pin_table_answer(
    spec: PartSpec, extraction: DatasheetExtraction, extraction_dir: Path
) -> tuple[tuple[float, float, float, float], str]:
    ref = spec.pin_table
    table = _table(extraction, extraction_dir, ref.page, ref.table)
    raw_rows = table.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("fixture pin table has no rows")
    rows = cast(list[object], raw_rows)
    boxes: list[tuple[float, float, float, float]] = []
    for pin in spec.pins:
        references = list((pin.reading.cells or {}).values())
        if not references:
            target_row = None
            for row_index, row in enumerate(rows):
                if not isinstance(row, list):
                    continue
                row_cells = cast(list[object], row)
                number = row_cells[ref.number_col] if ref.number_col < len(row_cells) else ""
                name = row_cells[ref.name_col] if ref.name_col < len(row_cells) else ""
                number_text = str(number or "").strip()
                if pin.number == number_text or (
                    spec.package.exposed_pad is not None
                    and pin.number == spec.package.exposed_pad.number
                    and not number_text
                    and "pad" in str(name).casefold()
                ):
                    target_row = row_index
                    break
            if target_row is None:
                raise ValueError(f"fixture pin table has no row for pin {pin.number}")
            pin.reading.cells = {
                "min": CellRef(table=ref.table, row=target_row, col=ref.number_col),
                "max": CellRef(table=ref.table, row=target_row, col=ref.name_col),
            }
            references = list(pin.reading.cells.values())
        for reference in references:
            cell = _cell_bbox(table, reference.row, reference.col)
            if cell is not None:
                boxes.append(cell)
    return _union(boxes), json.dumps(rows, ensure_ascii=False)


def _orderable_table_answer(
    spec: PartSpec, extraction: DatasheetExtraction, extraction_dir: Path
) -> tuple[tuple[float, float, float, float], str]:
    boxes: list[tuple[float, float, float, float]] = []
    selected_rows: list[list[str]] = []
    for variant in spec.orderable:
        table = _table(extraction, extraction_dir, variant.reading.page, variant.row.table)
        raw_rows = table.get("rows")
        if not isinstance(raw_rows, list):
            raise ValueError(f"fixture orderable row is unavailable for {variant.mpn}")
        rows = cast(list[object], raw_rows)
        if not 0 <= variant.row.row < len(rows):
            raise ValueError(f"fixture orderable row is unavailable for {variant.mpn}")
        row = rows[variant.row.row]
        if not isinstance(row, list):
            raise ValueError(f"fixture orderable row is malformed for {variant.mpn}")
        row_cells = cast(list[object], row)
        selected_rows.append([str(cell or "") for cell in row_cells])
        mpn_col = next(
            (
                index
                for index, cell in enumerate(row_cells)
                if str(cell or "").strip().casefold() == variant.mpn.strip().casefold()
            ),
            variant.row.col,
        )
        package_col = next(
            (
                index
                for index, cell in enumerate(row_cells)
                if str(cell or "").strip().casefold()
                == variant.package_designator.strip().casefold()
            ),
            mpn_col,
        )
        variant.reading.cells = {
            "min": CellRef(table=variant.row.table, row=variant.row.row, col=mpn_col),
            "max": CellRef(table=variant.row.table, row=variant.row.row, col=package_col),
        }
        for col in range(len(row_cells)):
            cell = _cell_bbox(table, variant.row.row, col)
            if cell is not None:
                boxes.append(cell)
    return _union(boxes), json.dumps(selected_rows, ensure_ascii=False)


def _replace_reading(spec: PartSpec, field: str, reading: Reading) -> None:
    if field == "package.pin1_reading":
        spec.package.pin1_reading = reading
    elif field.startswith("package.exposed_pad."):
        if spec.package.exposed_pad is None:
            raise ValueError("fixture exposed-pad reading has no exposed pad")
        dimension = field.rsplit(".", 1)[1]
        getattr(spec.package.exposed_pad, dimension).reading = reading
    elif field.startswith("package."):
        dimension = field.split(".", 1)[1]
        getattr(spec.package, dimension).reading = reading
    elif field.startswith("land_pattern.dimensions."):
        if spec.land_pattern is None:
            raise ValueError("fixture land-pattern reading has no land pattern")
        dimension = field.rsplit(".", 1)[1]
        spec.land_pattern.dimensions[dimension].reading = reading
    elif field.startswith("pins["):
        index = int(field[5:-1])
        spec.pins[index].reading = reading
    elif field.startswith("orderable["):
        index = int(field[10:-1])
        spec.orderable[index].reading = reading
    elif field == "pinout.view_reading" and spec.pinout is not None:
        spec.pinout.view_reading = reading
    else:
        raise ValueError(f"unsupported fixture reading field: {field}")


def attach_vision_reads(
    spec: PartSpec,
    spec_path: Path,
    extraction_path: Path,
    monkeypatch: pytest.MonkeyPatch | None = None,
    *,
    lane: Literal["a", "b"] = "b",
) -> PartSpec:
    if monkeypatch is None:
        with pytest.MonkeyPatch.context() as patcher:
            return attach_vision_reads(
                spec,
                spec_path,
                extraction_path,
                patcher,
                lane=lane,
            )
    extraction = DatasheetExtraction.model_validate_json(
        extraction_path.read_text(encoding="utf-8")
    )
    extraction_dir = extraction_path.resolve().parent
    seen_readings: set[int] = set()
    for field, reading, _ in _all_readings(spec):
        if id(reading) in seen_readings:
            reading = reading.model_copy(deep=True)
            _replace_reading(spec, field, reading)
        seen_readings.add(id(reading))
    _write_render_stub(monkeypatch, lane)

    pin_bbox, pin_table_answer = _pin_table_answer(spec, extraction, extraction_dir)
    orderable_bbox, orderable_answer = _orderable_table_answer(spec, extraction, extraction_dir)
    requests: list[tuple[VisionReadRequest, str, Callable[[str], None]]] = []
    for field, reading, _ in _all_readings(spec):
        if field == "package.pin1_reading":
            match = re.search(r"\b(?:top|bottom)[\s_-]+(?:left|right)\b", reading.vision, re.I)
            answer = match.group(0) if match is not None else reading.vision
            kind: Literal["pin1_corner", "view", "transcribe"] = "pin1_corner"
        elif field == "pinout.view_reading" and spec.pinout is not None:
            answer = spec.pinout.view
            kind = "view"
        else:
            answer = reading.vision
            kind = "transcribe"
        bbox = _reading_bbox(reading, extraction, extraction_dir)
        requests.append(
            (
                VisionReadRequest(field=field, page=reading.page, bbox=bbox, kind=kind),
                answer,
                lambda ref, selected=reading: setattr(selected, "vision_read", ref),
            )
        )

    requests.append(
        (
            VisionReadRequest(
                field="pin_table",
                page=spec.pin_table.page,
                bbox=pin_bbox,
                kind="table",
            ),
            pin_table_answer,
            lambda ref: setattr(spec.pin_table, "vision_read", ref),
        )
    )
    requests.append(
        (
            VisionReadRequest(
                field="orderable",
                page=spec.orderable[0].reading.page,
                bbox=orderable_bbox,
                kind="table",
            ),
            orderable_answer,
            lambda ref: setattr(spec, "orderable_vision_read", ref),
        )
    )
    if spec.pinout is not None:
        requests.append(
            (
                VisionReadRequest(
                    field="pinout.labels",
                    page=spec.pinout.page,
                    bbox=spec.pinout.bbox,
                    kind="pin_labels",
                ),
                json.dumps(spec.pinout.labels_vision, ensure_ascii=False),
                lambda ref: setattr(spec.pinout, "labels_vision_read", ref),
            )
        )

    for offset in range(0, len(requests), 7):
        chunk = requests[offset : offset + 7]
        out_dir = spec_path.resolve().parent / "vision-reads" / secrets.token_hex(6)
        batch = create_read_batch(
            extraction_path,
            [request for request, _, _ in chunk],
            out_dir=out_dir,
            lane=lane,
            profile="fixture",
            model="fixture-model",
        )
        answers: dict[str, dict[str, object]] = {}
        for item in batch.items:
            answer = (
                FIXTURE_CONTROL
                if item.control
                else next(answer for request, answer, _ in chunk if request.field == item.field)
            )
            answers[item.read_id] = {"answer": answer, "impression": FIXTURE_IMPRESSION}
        record = record_answers(out_dir / "batch.json", answers)
        if not record.control_passed:
            raise ValueError("fixture control answer did not pass")
        relative = out_dir.relative_to(spec_path.resolve().parent).as_posix()
        for item in batch.items:
            if item.control:
                continue
            setter = next(setter for request, _, setter in chunk if request.field == item.field)
            setter(f"{relative}/batch.json#{item.read_id}")
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec
