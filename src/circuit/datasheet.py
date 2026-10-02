"""Dual-lane extraction of PDF datasheets and deterministic page artifacts."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any, Literal, cast

import pdfplumber
from PIL import Image
from pydantic import BaseModel, ConfigDict

from .confidential import confidential_root_for, ensure_confidential_store
from .raster import RasterizeError, rasterize

if TYPE_CHECKING:
    from .humanrequest import HumanRequest
    from .partspec import SpecFinding

DRAWING_VECTOR_THRESHOLD = 500
_DRAWING_TEXT_MARKERS = (
    "PACKAGE OUTLINE",
    "PACKAGE DRAWING",
    "MECHANICAL DATA",
    "LAND PATTERN",
    "BOARD LAYOUT",
    "SOLDER MASK",
    "STENCIL",
)
_PDFTOTEXT_ENV = "CIRCUIT_PDFTOTEXT"
_TESSERACT_ENV = "CIRCUIT_TESSERACT"


class DatasheetError(RuntimeError):
    """Raised when a datasheet extraction cannot be completed safely."""


class PdfWord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    x0: float
    top: float
    x1: float
    bottom: float


class LaneResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lane: Literal["poppler", "pdfplumber", "ocr"]
    status: Literal["ok", "empty", "unavailable", "error"]
    words_path: str | None
    word_count: int
    detail: str = ""


class PageExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    width_pt: float
    height_pt: float
    png_path: str
    png_sha256: str
    dpi: int
    text_layer: bool
    lanes: list[LaneResult]
    tables_path: str | None
    table_count: int
    vector_objects: int
    drawing_page: bool
    order_similarity: float | None


class DatasheetExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_datasheet_extraction"]
    pdf_path: str
    pdf_sha256: str
    page_count: int
    pages: list[PageExtraction]
    tools: dict[str, str]


def word_has_ink(
    image_path: Path,
    word: PdfWord,
    *,
    dpi: int,
    width_pt: float,
    height_pt: float,
    minimum_fraction: float = 0.02,
) -> bool:
    try:
        with Image.open(image_path) as image:
            return _word_has_ink_in_image(
                image.convert("L"),
                word,
                dpi=dpi,
                width_pt=width_pt,
                height_pt=height_pt,
                minimum_fraction=minimum_fraction,
            )
    except (OSError, ValueError):
        return False


def words_by_ink(
    image_path: Path,
    words: Sequence[PdfWord],
    *,
    dpi: int,
    width_pt: float,
    height_pt: float,
    minimum_fraction: float = 0.02,
) -> tuple[list[PdfWord], list[PdfWord]]:
    visible: list[PdfWord] = []
    invisible: list[PdfWord] = []
    try:
        with Image.open(image_path) as image:
            gray = image.convert("L")
    except (OSError, ValueError):
        gray = None

    for word in words:
        center_x = (word.x0 + word.x1) / 2
        center_y = (word.top + word.bottom) / 2
        if not (0 <= center_x <= width_pt and 0 <= center_y <= height_pt):
            continue
        if gray is not None and _word_has_ink_in_image(
            gray,
            word,
            dpi=dpi,
            width_pt=width_pt,
            height_pt=height_pt,
            minimum_fraction=minimum_fraction,
        ):
            visible.append(word)
        else:
            invisible.append(word)
    return visible, invisible


def _word_has_ink_in_image(
    gray: Image.Image,
    word: PdfWord,
    *,
    dpi: int,
    width_pt: float,
    height_pt: float,
    minimum_fraction: float,
) -> bool:
    center_x = (word.x0 + word.x1) / 2
    center_y = (word.top + word.bottom) / 2
    if not (0 <= center_x <= width_pt and 0 <= center_y <= height_pt):
        return False
    scale = dpi / 72
    try:
        left = max(0, int(word.x0 * scale))
        top = max(0, int(word.top * scale))
        right = min(gray.width, math.ceil(word.x1 * scale))
        bottom = min(gray.height, math.ceil(word.bottom * scale))
        if right <= left or bottom <= top:
            return False
        crop = gray.crop((left, top, right, bottom))
        pixel_count = crop.width * crop.height
        if pixel_count == 0:
            return False
        histogram = crop.histogram()
        return sum(histogram[:128]) / pixel_count >= minimum_fraction
    except ValueError:
        return False


def _drawing_page(
    vector_objects: int,
    words_by_lane: Sequence[list[PdfWord]],
    image_path: Path,
    *,
    dpi: int,
    width_pt: float,
    height_pt: float,
) -> bool:
    if vector_objects >= DRAWING_VECTOR_THRESHOLD:
        return True
    for words in words_by_lane:
        visible_words, _ = words_by_ink(
            image_path,
            words,
            dpi=dpi,
            width_pt=width_pt,
            height_pt=height_pt,
        )
        visible_words.sort(key=lambda word: (word.top, word.x0))
        text = " ".join(word.text for word in visible_words)
        if any(marker in text.upper() for marker in _DRAWING_TEXT_MARKERS) or re.search(
            r"\bSCALE\b", text, re.IGNORECASE
        ):
            return True
    return False


def _command(env_name: str, default: str) -> list[str]:
    return shlex.split(os.environ.get(env_name, default))


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _tool_version(command: list[str], *, stderr: bool = False) -> str:
    try:
        result = _run(command)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    output = result.stderr if stderr else result.stdout
    if not output.strip() and stderr:
        output = result.stdout
    return output.splitlines()[0].strip() if output.strip() else "unavailable"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _relative(path: Path, out_dir: Path) -> str:
    return path.relative_to(out_dir).as_posix()


def _poppler_words(pdf_path: Path, page_number: int) -> tuple[list[PdfWord], str]:
    command = _command(_PDFTOTEXT_ENV, "pdftotext")
    with tempfile.TemporaryDirectory(prefix="circuit-pdftotext-") as temporary:
        html_path = Path(temporary) / "page.html"
        try:
            result = _run(
                [
                    *command,
                    "-bbox-layout",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    str(pdf_path),
                    str(html_path),
                ]
            )
        except FileNotFoundError:
            return [], f"{command[0]} is not available; install poppler-utils"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], f"pdftotext failed: {exc}"
        if result.returncode:
            return [], result.stderr.strip() or f"pdftotext exited with {result.returncode}"
        try:
            root = ET.parse(html_path).getroot()
            words: list[PdfWord] = []
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "word":
                    continue
                words.append(
                    PdfWord(
                        text="".join(element.itertext()),
                        x0=float(element.attrib["xMin"]),
                        top=float(element.attrib["yMin"]),
                        x1=float(element.attrib["xMax"]),
                        bottom=float(element.attrib["yMax"]),
                    )
                )
        except (OSError, ET.ParseError, KeyError, ValueError) as exc:
            return [], f"could not parse pdftotext XHTML: {exc}"
        return words, ""


def _is_rotated_char(char: Mapping[str, Any]) -> bool:
    matrix = cast(Sequence[Any] | None, char.get("matrix"))
    if not isinstance(matrix, (tuple, list)) or len(matrix) < 6:
        return False
    a, b = matrix[0], matrix[1]
    return (
        isinstance(a, (int, float))
        and isinstance(b, (int, float))
        and abs(float(b)) > abs(float(a))
    )


def _rotated_char_words(chars: Sequence[Mapping[str, Any]]) -> list[PdfWord]:
    columns: dict[str, list[dict[str, Any]]] = {"btt": [], "ttb": []}
    for char in chars:
        if not _is_rotated_char(char):
            continue
        matrix = cast(Sequence[float], char["matrix"])
        direction = "btt" if matrix[1] > 0 else "ttb"
        columns[direction].append(dict(char))

    words: list[PdfWord] = []
    for direction, rotated in columns.items():
        ordered_chars = sorted(
            rotated,
            key=lambda char: (
                (float(char["x0"]) + float(char["x1"])) / 2,
                float(char["top"]),
            ),
        )
        grouped_columns: list[list[dict[str, Any]]] = []
        for char in ordered_chars:
            center_x = (float(char["x0"]) + float(char["x1"])) / 2
            size = float(char.get("size") or (float(char["bottom"]) - float(char["top"])))
            matching = [
                column
                for column in grouped_columns
                if abs(
                    center_x
                    - median((float(item["x0"]) + float(item["x1"])) / 2 for item in column)
                )
                <= 0.3
                * min(
                    size,
                    median(
                        float(item.get("size") or (float(item["bottom"]) - float(item["top"])))
                        for item in column
                    ),
                )
            ]
            if matching:
                min(
                    matching,
                    key=lambda column: abs(
                        center_x
                        - median((float(item["x0"]) + float(item["x1"])) / 2 for item in column)
                    ),
                ).append(char)
            else:
                grouped_columns.append([char])

        for column in grouped_columns:
            column.sort(key=lambda char: (float(char["top"]), float(char["x0"])))
            groups: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            for char in column:
                text = str(char.get("text", ""))
                if text.isspace():
                    if current:
                        groups.append(current)
                        current = []
                    continue
                if current:
                    previous = current[-1]
                    previous_size = float(
                        previous.get("size") or (float(previous["bottom"]) - float(previous["top"]))
                    )
                    size = float(char.get("size") or (float(char["bottom"]) - float(char["top"])))
                    gap = float(char["top"]) - float(previous["bottom"])
                    if gap > 0.5 * min(previous_size, size):
                        groups.append(current)
                        current = []
                current.append(char)
            if current:
                groups.append(current)

            for group in groups:
                ordered = group if direction == "ttb" else list(reversed(group))
                words.append(
                    PdfWord(
                        text="".join(str(char.get("text", "")) for char in ordered),
                        x0=min(float(char["x0"]) for char in group),
                        top=min(float(char["top"]) for char in group),
                        x1=max(float(char["x1"]) for char in group),
                        bottom=max(float(char["bottom"]) for char in group),
                    )
                )
    return sorted(words, key=lambda word: (word.top, word.x0))


def _pdfplumber_words(page: Any) -> list[PdfWord]:
    words: list[PdfWord] = []

    def is_normal_char(char: Mapping[str, Any]) -> bool:
        return not _is_rotated_char(char)

    filtered_page = page.filter(is_normal_char)
    for word in cast(list[dict[str, Any]], filtered_page.extract_words()):
        words.append(
            PdfWord(
                text=str(word["text"]),
                x0=float(word["x0"]),
                top=float(word["top"]),
                x1=float(word["x1"]),
                bottom=float(word["bottom"]),
            )
        )
    words.extend(_rotated_char_words(cast(Sequence[Mapping[str, Any]], page.chars)))
    return sorted(words, key=lambda word: (word.top, word.x0))


def _ocr_words(png_path: Path, dpi: int) -> tuple[list[PdfWord], str]:
    command = _command(_TESSERACT_ENV, "tesseract")
    try:
        result = _run([*command, str(png_path), "stdout", "--psm", "3", "tsv"])
    except FileNotFoundError:
        return [], f"{command[0]} is not available; install tesseract-ocr"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"tesseract failed: {exc}"
    if result.returncode:
        return [], result.stderr.strip() or f"tesseract exited with {result.returncode}"
    words: list[PdfWord] = []
    try:
        rows = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
        for row in rows:
            if row.get("level") != "5" or not (text := (row.get("text") or "").strip()):
                continue
            left = float(row["left"])
            top = float(row["top"])
            width = float(row["width"])
            height = float(row["height"])
            factor = 72 / dpi
            words.append(
                PdfWord(
                    text=text,
                    x0=left * factor,
                    top=top * factor,
                    x1=(left + width) * factor,
                    bottom=(top + height) * factor,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        return [], f"could not parse tesseract TSV: {exc}"
    return words, ""


def _write_lane(
    out_dir: Path,
    page_number: int,
    lane: Literal["poppler", "pdfplumber", "ocr"],
    words: list[PdfWord],
    status: Literal["ok", "empty", "unavailable", "error"],
    detail: str = "",
) -> LaneResult:
    path = out_dir / f"page-{page_number:03d}.{lane}.json"
    _write_json(path, [word.model_dump(mode="json") for word in words])
    return LaneResult(
        lane=lane,
        status=status,
        words_path=_relative(path, out_dir),
        word_count=len(words),
        detail=detail,
    )


def _status(words: list[PdfWord], detail: str) -> Literal["ok", "empty", "unavailable", "error"]:
    if detail:
        return "unavailable" if "is not available" in detail else "error"
    return "ok" if words else "empty"


def extract_datasheet(
    pdf_path: Path,
    out_dir: Path,
    *,
    pages: Sequence[int] | None = None,
    dpi: int = 300,
) -> DatasheetExtraction:
    """Extract text, tables, and page images from a PDF datasheet."""
    if not pdf_path.is_file():
        raise DatasheetError(f"datasheet file is missing: {pdf_path}")
    try:
        with pdf_path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise DatasheetError(f"not a PDF file: {pdf_path}")
    except OSError as exc:
        raise DatasheetError(f"could not read datasheet {pdf_path}: {exc}") from exc
    if not 72 <= dpi <= 1200:
        raise DatasheetError("dpi must be between 72 and 1200")

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as exc:
        raise DatasheetError(f"could not open PDF (possibly encrypted): {exc}") from exc

    with pdf:
        try:
            page_count = len(pdf.pages)
        except Exception as exc:
            raise DatasheetError(f"could not read PDF pages: {exc}") from exc
        if page_count == 0:
            raise DatasheetError("PDF contains no pages")
        selected_pages = list(pages) if pages is not None else list(range(1, page_count + 1))
        if any(page < 1 or page > page_count for page in selected_pages):
            raise DatasheetError(f"requested page is outside 1..{page_count}")
        if len(set(selected_pages)) != len(selected_pages):
            raise DatasheetError("requested pages must be unique")
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            pdf_digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise DatasheetError(f"could not read datasheet {pdf_path}: {exc}") from exc
        poppler_command = _command(_PDFTOTEXT_ENV, "pdftotext")
        tesseract_command = _command(_TESSERACT_ENV, "tesseract")
        tools = {
            "pdfplumber": pdfplumber.__version__,
            "pdftotext": _tool_version([*poppler_command, "-v"], stderr=True),
            "tesseract": _tool_version([*tesseract_command, "--version"]),
        }
        extracted_pages: list[PageExtraction] = []
        for page_number in selected_pages:
            image_path = out_dir / f"page-{page_number:03d}.png"
            try:
                with tempfile.TemporaryDirectory(prefix=".raster-", dir=out_dir) as temporary:
                    images = rasterize(
                        pdf_path,
                        Path(temporary),
                        dpi=dpi,
                        first_page=page_number,
                        last_page=page_number,
                    )
                    if len(images) != 1:
                        raise DatasheetError(
                            f"rasterizer returned {len(images)} images for page {page_number}"
                        )
                    shutil.copyfile(images[0], image_path)
            except (RasterizeError, OSError) as exc:
                raise DatasheetError(f"could not rasterize page {page_number}: {exc}") from exc

            page = cast(Any, pdf.pages[page_number - 1])
            poppler_words, poppler_detail = _poppler_words(pdf_path, page_number)
            try:
                plumber_words = _pdfplumber_words(page)
                tables: list[dict[str, object]] = []
                for table in page.find_tables():
                    rows = table.extract()
                    tables.append(
                        {
                            "bbox": [float(value) for value in table.bbox],
                            "rows": rows,
                            "cells": [
                                [
                                    None if cell is None else [float(value) for value in cell]
                                    for cell in row.cells
                                ]
                                for row in table.rows
                            ],
                        }
                    )
                vector_objects = len(page.lines) + len(page.rects) + len(page.curves)
            except Exception as exc:
                raise DatasheetError(f"could not read PDF page {page_number}: {exc}") from exc
            plumber_detail = ""
            try:
                width_pt = float(page.width)
                height_pt = float(page.height)
                has_chars = bool(page.chars)
            except Exception as exc:
                raise DatasheetError(f"could not read PDF page {page_number}: {exc}") from exc
            is_drawing_page = _drawing_page(
                vector_objects,
                [poppler_words, plumber_words],
                image_path,
                dpi=dpi,
                width_pt=width_pt,
                height_pt=height_pt,
            )
            table_path = out_dir / f"page-{page_number:03d}.tables.json"
            _write_json(table_path, {"tables": tables})
            lanes = [
                _write_lane(
                    out_dir,
                    page_number,
                    "poppler",
                    poppler_words,
                    _status(poppler_words, poppler_detail),
                    poppler_detail,
                ),
                _write_lane(
                    out_dir,
                    page_number,
                    "pdfplumber",
                    plumber_words,
                    _status(plumber_words, plumber_detail),
                    plumber_detail,
                ),
            ]
            text_layer = has_chars or bool(poppler_words)
            ocr_words: list[PdfWord] = []
            if not text_layer:
                ocr_words, ocr_detail = _ocr_words(image_path, dpi)
                lanes.append(
                    _write_lane(
                        out_dir,
                        page_number,
                        "ocr",
                        ocr_words,
                        _status(ocr_words, ocr_detail),
                        ocr_detail,
                    )
                )
            similarity: float | None = None
            poppler_lane = lanes[0]
            plumber_lane = lanes[1]
            if poppler_lane.status in ("ok", "empty") and plumber_lane.status in ("ok", "empty"):
                similarity = round(
                    difflib.SequenceMatcher(
                        None,
                        [word.text for word in poppler_words],
                        [word.text for word in plumber_words],
                        autojunk=False,
                    ).ratio(),
                    4,
                )
            extracted_pages.append(
                PageExtraction(
                    page=page_number,
                    width_pt=width_pt,
                    height_pt=height_pt,
                    png_path=_relative(image_path, out_dir),
                    png_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    dpi=dpi,
                    text_layer=text_layer,
                    lanes=lanes,
                    tables_path=_relative(table_path, out_dir),
                    table_count=len(tables),
                    vector_objects=vector_objects,
                    drawing_page=is_drawing_page,
                    order_similarity=similarity,
                )
            )

    extraction = DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path=str(pdf_path),
        pdf_sha256=pdf_digest,
        page_count=page_count,
        pages=extracted_pages,
        tools=tools,
    )
    _write_json(out_dir / "extraction.json", extraction.model_dump(mode="json"))
    return extraction


def load_extraction(path: Path) -> DatasheetExtraction:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return DatasheetExtraction.model_validate(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise DatasheetError(f"could not load datasheet extraction {path}: {exc}") from exc


def _read_words(extraction_dir: Path, lane: LaneResult) -> list[PdfWord]:
    if lane.status != "ok" or lane.words_path is None:
        return []
    try:
        value = json.loads((extraction_dir / lane.words_path).read_text(encoding="utf-8"))
        if not isinstance(value, list):
            return []
        return [PdfWord.model_validate(item) for item in cast(list[object], value)]
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def page_words(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: int,
    lanes: Sequence[Literal["poppler", "pdfplumber", "ocr"]] = (
        "poppler",
        "pdfplumber",
        "ocr",
    ),
) -> list[PdfWord]:
    """Load successful lane words for a page in the requested lane order."""
    page_result = next((item for item in extraction.pages if item.page == page), None)
    if page_result is None:
        return []
    by_lane = {item.lane: item for item in page_result.lanes}
    return [
        word
        for lane_name in lanes
        if (lane := by_lane.get(lane_name)) is not None
        for word in _read_words(extraction_dir, lane)
    ]


def page_tables(
    extraction: DatasheetExtraction, extraction_dir: Path, page: int
) -> list[list[list[str | None]]]:
    """Load extracted table rows for a page."""
    page_result = next((item for item in extraction.pages if item.page == page), None)
    if page_result is None or page_result.tables_path is None:
        return []
    try:
        value: object = json.loads(
            (extraction_dir / page_result.tables_path).read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            return []
        table_container = cast(dict[str, object], value)
        raw_tables = table_container.get("tables", [])
        if not isinstance(raw_tables, list):
            return []
        tables: list[list[list[str | None]]] = []
        for raw_table in cast(list[object], raw_tables):
            if not isinstance(raw_table, dict):
                continue
            table = cast(dict[str, object], raw_table)
            rows = table.get("rows")
            if isinstance(rows, list):
                tables.append(cast(list[list[str | None]], rows))
        return tables
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return []


def check_received(pdf: Path, request: HumanRequest) -> list[SpecFinding]:
    """Check both text extraction lanes against a datasheet acquisition request."""
    if request.kind != "datasheet_acquisition":
        raise DatasheetError("received datasheet checks require a datasheet acquisition request")

    request_data = cast(dict[str, object], request.model_dump(mode="python"))
    subject = cast(dict[str, object], request_data["subject"])
    details = cast(dict[str, object], request_data["details"])
    mpn = str(subject["mpn"]).casefold()
    revision = details.get("requested_revision")
    required_sections = cast(list[str], details["required_sections"])
    private_root = confidential_root_for(pdf)
    if private_root is None:
        with tempfile.TemporaryDirectory(prefix="circuit-received-") as temporary:
            return _check_received_in_dir(
                pdf,
                Path(temporary),
                mpn=mpn,
                revision=revision if isinstance(revision, str) else None,
                required_sections=required_sections,
            )
    ensure_confidential_store(private_root.parent)
    check_root = private_root / "received-checks"
    check_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="circuit-received-", dir=check_root) as temporary:
        return _check_received_in_dir(
            pdf,
            Path(temporary),
            mpn=mpn,
            revision=revision if isinstance(revision, str) else None,
            required_sections=required_sections,
        )


def _check_received_in_dir(
    pdf: Path,
    output_dir: Path,
    *,
    mpn: str,
    revision: str | None,
    required_sections: list[str],
) -> list[SpecFinding]:
    from .partspec import SpecFinding

    try:
        extraction = extract_datasheet(pdf, output_dir)
    except DatasheetError as exc:
        return [
            SpecFinding(
                code="datasheet_extraction_failed",
                severity="error",
                field="datasheet",
                message=str(exc),
            )
        ]
    lane_text: dict[str, str] = {}
    for lane in ("poppler", "pdfplumber"):
        lane_text[lane] = " ".join(
            word.text
            for page in extraction.pages
            for word in page_words(extraction, output_dir, page.page, lanes=(lane,))
        ).casefold()

    findings: list[SpecFinding] = []
    ordering_terms = ("ordering information", "orderable", "ordering")
    missing_mpn_lanes = [
        lane
        for lane, text in lane_text.items()
        if mpn not in text or not any(term in text for term in ordering_terms)
    ]
    if missing_mpn_lanes:
        findings.append(
            SpecFinding(
                code="datasheet_mpn_mismatch",
                severity="error",
                field="datasheet.mpn",
                message=(
                    "target MPN or ordering information is missing from extraction lanes: "
                    + ", ".join(missing_mpn_lanes)
                ),
            )
        )
    if revision is not None:
        missing_revision_lanes = [
            lane for lane, text in lane_text.items() if revision.casefold() not in text
        ]
        if missing_revision_lanes:
            findings.append(
                SpecFinding(
                    code="datasheet_revision_mismatch",
                    severity="error",
                    field="datasheet.revision",
                    message=(
                        f"requested revision {revision!r} is missing from extraction lanes: "
                        + ", ".join(missing_revision_lanes)
                    ),
                )
            )
    section_keywords = {
        "package_drawing": (
            "package outline",
            "package drawing",
            "package dimensions",
            "mechanical data",
        ),
        "pinout": ("pinout", "pin configuration", "top view", "bottom view"),
        "pin_table": ("pin description", "pin functions", "pin name", "pin number"),
        "land_pattern": (
            "land pattern",
            "recommended land pattern",
            "pcb layout",
        ),
        "orderable_table": ("ordering information", "orderable", "ordering"),
    }
    for section in required_sections:
        keywords = section_keywords[section]
        missing_lanes = [
            lane
            for lane, text in lane_text.items()
            if not any(keyword in text for keyword in keywords)
        ]
        if missing_lanes:
            findings.append(
                SpecFinding(
                    code=f"datasheet_section_missing:{section}",
                    severity="error",
                    field=f"datasheet.sections.{section}",
                    message=(
                        "required section is missing from extraction lanes: "
                        + ", ".join(missing_lanes)
                    ),
                )
            )
    return findings
