from __future__ import annotations

import hashlib
import itertools
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pdfplumber
from pydantic import BaseModel, ConfigDict

from . import datasheet, occt
from .datasheet import PdfWord

if TYPE_CHECKING:
    from .libitems import FootprintDef
    from .libverify import VerifyFinding
    from .occt import Shape
    from .partspec import PartSpec

_ROW_TOLERANCE_PT = 2.0
_DESIGNATOR = re.compile(r"^[A-Z0-9]{1,8}$")
_DRAWING_TOKEN = re.compile(r"^[A-Z0-9]{1,16}$")
_INTEGER = re.compile(r"^\d+$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_DRAWING_MARKERS = (
    "package outline",
    "mechanical data",
    "package dimensions",
    "package drawing",
)
_TOLERANCE_MM = 0.05


class PackageIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mpn: str
    row_pages: list[int]
    designator: str
    pin_count_candidates: list[int]
    drawing_page: int
    drawing_id: str | None
    body_ranges_mm: list[tuple[float, float]]


def _finding(code: str, subject: str, message: str) -> VerifyFinding:
    from .libverify import VerifyFinding

    return VerifyFinding(code=code, severity="error", subject=subject, message=message)


def _row_groups(words: list[PdfWord], page: int) -> list[tuple[int, list[PdfWord]]]:
    rows: list[tuple[float, list[PdfWord]]] = []
    for word in sorted(words, key=lambda item: (item.bottom, item.x0)):
        match = next(
            (
                index
                for index, (baseline, _) in enumerate(rows)
                if abs(baseline - word.bottom) <= _ROW_TOLERANCE_PT
            ),
            None,
        )
        if match is None:
            rows.append((word.bottom, [word]))
        else:
            rows[match][1].append(word)
    return [
        (page, sorted(row, key=lambda item: item.x0))
        for _, row in sorted(rows, key=lambda item: item[0])
    ]


def _drawing_page(words: list[PdfWord]) -> bool:
    text = " ".join(word.text for word in words).casefold()
    return any(marker in text for marker in _DRAWING_MARKERS)


def _drawing_ids(words: list[PdfWord]) -> set[str]:
    return {
        token
        for word in words
        for token in re.findall(r"[A-Za-z0-9]+", word.text)
        if _DRAWING_TOKEN.fullmatch(token.upper())
    }


def _bracketed_numbers(rows: list[tuple[int, list[PdfWord]]]) -> set[int]:
    indices: set[int] = set()
    flat = [word for _, row in rows for word in row]
    bracketed = False
    for index, word in enumerate(flat):
        if "[" in word.text:
            bracketed = True
        if bracketed:
            indices.add(index)
        if "]" in word.text:
            bracketed = False
    return indices


def _body_ranges(
    rows: list[tuple[int, list[PdfWord]]],
    words: list[PdfWord],
) -> list[tuple[float, float]]:
    if not any("mm" in word.text.casefold() for word in words):
        return []
    bracketed = _bracketed_numbers(rows)

    pairs: set[tuple[float, float]] = set()
    numeric_by_row: list[list[tuple[int, PdfWord, float]]] = []
    flat_index = 0
    for _, row in rows:
        numeric: list[tuple[int, PdfWord, float]] = []
        for word in row:
            text = word.text.strip()
            if flat_index not in bracketed and _NUMBER.fullmatch(text) and float(text) > 0:
                numeric.append((flat_index, word, float(text)))
            flat_index += 1
        numeric_by_row.append(numeric)

    for numeric in numeric_by_row:
        for left, right in itertools.pairwise(numeric):
            low, high = sorted((left[2], right[2]))
            if high <= 100:
                pairs.add((low, high))

    for first, second in itertools.pairwise(numeric_by_row):
        if not first or not second:
            continue
        for _, left_word, left_value in first:
            for _, right_word, right_value in second:
                if abs(left_word.x0 - right_word.x0) <= 2.5:
                    low, high = sorted((left_value, right_value))
                    if high <= 100:
                        pairs.add((low, high))
    return sorted(pairs)


def _row_signature(
    rows: list[tuple[int, list[PdfWord]]],
    mpn: str,
) -> tuple[tuple[int | str, ...], ...]:
    return tuple(
        sorted(
            (
                page,
                *(word.text for word in row),
            )
            for page, row in rows
            if any(word.text == mpn for word in row)
        )
    )


def _resolve_lane(
    pages: dict[int, list[PdfWord]],
    mpn: str,
) -> tuple[PackageIdentity | None, str | None, str]:
    rows_by_page = {page: _row_groups(words, page) for page, words in pages.items()}
    orderable_rows = [
        row
        for rows in rows_by_page.values()
        for row in rows
        if any(word.text == mpn for word in row[1])
    ]
    if not orderable_rows:
        return None, "package_identity_mpn_unresolved", "MPN was not found as an exact PDF token"

    drawing_pages = {page: words for page, words in pages.items() if _drawing_page(words)}
    all_drawing_ids = {page: _drawing_ids(words) for page, words in drawing_pages.items()}
    designators: set[str] = set()
    pin_counts: set[int] = set()
    for _, row in orderable_rows:
        tokens = [word.text for word in row]
        for token in tokens:
            if (
                token != mpn
                and _DESIGNATOR.fullmatch(token)
                and (token.isupper() or token.isdigit())
                and any(
                    drawing_id == token or drawing_id.startswith(token)
                    for drawing_ids in all_drawing_ids.values()
                    for drawing_id in drawing_ids
                )
            ):
                designators.add(token)
            if _INTEGER.fullmatch(token) and int(token) > 0:
                pin_counts.add(int(token))
    if len(designators) > 1:
        return (
            None,
            "package_identity_ambiguous",
            "MPN rows resolve to multiple package designators",
        )
    if not designators:
        return (
            None,
            "package_identity_drawing_unresolved",
            "no orderable-row designator matches a package drawing page",
        )

    designator = next(iter(designators))
    matching_pages = {
        page: words
        for page, words in drawing_pages.items()
        if any(
            drawing_id == designator or drawing_id.startswith(designator)
            for drawing_id in all_drawing_ids[page]
        )
    }
    if len(matching_pages) != 1:
        return (
            None,
            "package_identity_ambiguous",
            "package designator does not resolve to one drawing page",
        )
    drawing_page, drawing_words = next(iter(matching_pages.items()))
    drawing_rows = rows_by_page[drawing_page]
    body_ranges = _body_ranges(drawing_rows, drawing_words)
    drawing_ids = sorted(
        (
            item
            for item in all_drawing_ids[drawing_page]
            if item == designator or item.startswith(designator)
        ),
        key=lambda item: (-len(item), item),
    )
    return (
        PackageIdentity(
            mpn=mpn,
            row_pages=sorted({page for page, _ in orderable_rows}),
            designator=designator,
            pin_count_candidates=sorted(pin_counts),
            drawing_page=drawing_page,
            drawing_id=drawing_ids[0] if drawing_ids else None,
            body_ranges_mm=body_ranges,
        ),
        None,
        "",
    )


def _extract_lane(
    pdf_path: Path,
    *,
    poppler: bool,
) -> tuple[dict[int, list[PdfWord]], str | None]:
    pages: dict[int, list[PdfWord]] = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                if poppler:
                    words, detail = datasheet._poppler_words(  # pyright: ignore[reportPrivateUsage]
                        pdf_path, page_number
                    )
                    if detail:
                        return {}, detail
                else:
                    words = datasheet._pdfplumber_words(  # pyright: ignore[reportPrivateUsage]
                        cast(Any, page)
                    )
                pages[page_number] = words
    except Exception as exc:
        return {}, str(exc)
    return pages, None


def resolve_package_identity(
    pdf_path: Path,
    mpn: str,
) -> tuple[PackageIdentity | None, list[VerifyFinding]]:
    """Resolve package facts from two fresh PDF text-extraction lanes."""

    poppler_pages, poppler_error = _extract_lane(pdf_path, poppler=True)
    plumber_pages, plumber_error = _extract_lane(pdf_path, poppler=False)
    if poppler_error is not None or plumber_error is not None:
        return None, [
            _finding(
                "package_identity_lane_mismatch",
                "datasheet",
                "Poppler and pdfplumber could not both extract the current PDF "
                f"(Poppler: {poppler_error or 'ok'}; pdfplumber: {plumber_error or 'ok'})",
            )
        ]

    poppler_rows = [
        row
        for page, words in poppler_pages.items()
        for row in _row_groups(words, page)
        if any(word.text == mpn for word in row[1])
    ]
    plumber_rows = [
        row
        for page, words in plumber_pages.items()
        for row in _row_groups(words, page)
        if any(word.text == mpn for word in row[1])
    ]
    if not poppler_rows and not plumber_rows:
        return None, [
            _finding(
                "package_identity_mpn_unresolved",
                "datasheet",
                f"MPN {mpn!r} was not found as an exact token in the current PDF",
            )
        ]
    if _row_signature(poppler_rows, mpn) != _row_signature(plumber_rows, mpn):
        return None, [
            _finding(
                "package_identity_lane_mismatch",
                "datasheet",
                "Poppler and pdfplumber disagree about the MPN orderable row",
            )
        ]

    poppler_identity, poppler_code, poppler_message = _resolve_lane(poppler_pages, mpn)
    plumber_identity, plumber_code, plumber_message = _resolve_lane(plumber_pages, mpn)
    if poppler_identity is not None and plumber_identity is not None:
        if poppler_identity == plumber_identity:
            return poppler_identity, []
    elif poppler_code == plumber_code and poppler_code is not None:
        return None, [_finding(poppler_code, "datasheet", poppler_message)]
    return None, [
        _finding(
            "package_identity_lane_mismatch",
            "datasheet",
            "Poppler and pdfplumber resolve different package identities "
            f"(Poppler: {poppler_message or 'resolved'}; "
            f"pdfplumber: {plumber_message or 'resolved'})",
        )
    ]


def sibling_package_mpn(pdf_path: Path, mpn: str) -> str | None:
    current, findings = resolve_package_identity(pdf_path, mpn)
    if current is None or findings:
        return None
    pages, error = _extract_lane(pdf_path, poppler=True)
    if error is not None:
        return None
    ids_by_page = {
        page: _drawing_ids(words) for page, words in pages.items() if _drawing_page(words)
    }
    candidates: list[str] = []
    for page, words in pages.items():
        for _, row in _row_groups(words, page):
            tokens = [word.text for word in row]
            designators = {
                token
                for token in tokens
                if token != mpn
                and _DESIGNATOR.fullmatch(token)
                and (token.isupper() or token.isdigit())
                and any(
                    drawing_id == token or drawing_id.startswith(token)
                    for drawing_ids in ids_by_page.values()
                    for drawing_id in drawing_ids
                )
            }
            if not any(item != current.designator for item in designators):
                continue
            candidates.extend(
                token
                for token in tokens
                if token != mpn
                and len(token) >= 4
                and any(char.isalpha() for char in token)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+./-]*", token)
            )
    for candidate in candidates:
        if candidate in {mpn, current.mpn}:
            continue
        identity, candidate_findings = resolve_package_identity(pdf_path, candidate)
        if (
            identity is not None
            and not candidate_findings
            and identity.designator != current.designator
        ):
            return candidate
    return None


def _range_matches(extent: float, ranges: list[tuple[float, float]]) -> bool:
    return any(low - _TOLERANCE_MM <= extent <= high + _TOLERANCE_MM for low, high in ranges)


def _body_matches(
    extents: tuple[float, float],
    ranges: list[tuple[float, float]],
) -> bool:
    if not ranges:
        return False
    for first, second in itertools.combinations_with_replacement(ranges, 2):
        if (_range_matches(extents[0], [first]) and _range_matches(extents[1], [second])) or (
            _range_matches(extents[0], [second]) and _range_matches(extents[1], [first])
        ):
            return True
    return False


def _thermal_pad_in_footprint(footprint: FootprintDef) -> bool:
    areas = sorted(
        (
            pad.width * pad.height
            for pad in footprint.pads
            if pad.type != "np_thru_hole" and any(layer.endswith(".Cu") for layer in pad.layers)
        ),
        reverse=True,
    )
    return len(areas) > 1 and areas[0] > areas[1] * 1.5


def _pin_count_matches(count: int, candidates: list[int], *, thermal_pad: bool) -> bool:
    return count in candidates or (
        thermal_pad and any(abs(count - candidate) == 1 for candidate in candidates)
    )


def _footprint_body_extents(footprint: FootprintDef) -> tuple[float, float] | None:
    graphics = [item for item in footprint.graphics if item.layer == "F.Fab" and item.points]
    if not graphics:
        return None
    x_min = min(point[0] - graphic.width / 2 for graphic in graphics for point in graphic.points)
    y_min = min(point[1] - graphic.width / 2 for graphic in graphics for point in graphic.points)
    x_max = max(point[0] + graphic.width / 2 for graphic in graphics for point in graphic.points)
    y_max = max(point[1] + graphic.width / 2 for graphic in graphics for point in graphic.points)
    return x_max - x_min, y_max - y_min


def _model_body_extents(shape: Shape) -> tuple[float, float] | None:
    facts = occt.inspect(shape)
    if facts.units != "mm" or not facts.solids:
        return None
    body = max(facts.solids, key=lambda solid: solid.volume)
    return body.bbox.x_max - body.bbox.x_min, body.bbox.y_max - body.bbox.y_min


def check_package_identity(
    spec: PartSpec,
    footprint: FootprintDef,
    model: Shape | None,
    *,
    pdf_path: Path,
) -> list[VerifyFinding]:
    findings: list[VerifyFinding] = []
    try:
        actual_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    except OSError as exc:
        return [
            _finding(
                "package_identity_pdf_hash_mismatch",
                "datasheet",
                f"could not read the PDF for package identity verification: {exc}",
            )
        ]
    if actual_sha256 != spec.datasheet.sha256:
        return [
            _finding(
                "package_identity_pdf_hash_mismatch",
                "datasheet",
                "current PDF SHA-256 differs from the PartSpec datasheet binding",
            )
        ]

    identity, resolution_findings = resolve_package_identity(pdf_path, spec.mpn)
    findings.extend(resolution_findings)
    if identity is None:
        return findings

    footprint_numbers = {
        pad.number
        for pad in footprint.pads
        if pad.number
        and pad.type != "np_thru_hole"
        and any(layer.endswith(".Cu") for layer in pad.layers)
    }
    if not _pin_count_matches(
        len(footprint_numbers),
        identity.pin_count_candidates,
        thermal_pad=_thermal_pad_in_footprint(footprint),
    ):
        findings.append(
            _finding(
                "package_identity_pin_count_mismatch",
                "footprint",
                f"{len(footprint_numbers)} distinct numbered copper pads do not match "
                f"PDF pin-count candidates {identity.pin_count_candidates}",
            )
        )
    if model is not None:
        try:
            regions = occt.slab_regions(model, -0.005, 0.015)
            model_terminal_count = len(regions)
            model_thermal_pad = (
                len(regions) > 1
                and max(region.area for region in regions)
                > sorted((region.area for region in regions), reverse=True)[1] * 1.5
            )
        except Exception:
            model_terminal_count = 0
            model_thermal_pad = False
        if not _pin_count_matches(
            model_terminal_count,
            identity.pin_count_candidates,
            thermal_pad=model_thermal_pad,
        ):
            findings.append(
                _finding(
                    "package_identity_pin_count_mismatch",
                    "model",
                    f"{model_terminal_count} model terminal regions do not match "
                    f"PDF pin-count candidates {identity.pin_count_candidates}",
                )
            )

    footprint_extents = _footprint_body_extents(footprint)
    if footprint_extents is None or not _body_matches(footprint_extents, identity.body_ranges_mm):
        findings.append(
            _finding(
                "package_identity_body_mismatch",
                "footprint",
                "F.Fab body outline extents do not fit dimensions extracted from "
                "the package drawing",
            )
        )
    if model is not None:
        try:
            model_extents = _model_body_extents(model)
        except Exception:
            model_extents = None
        if model_extents is None or not _body_matches(model_extents, identity.body_ranges_mm):
            findings.append(
                _finding(
                    "package_identity_body_mismatch",
                    "model",
                    "model body-solid extents do not fit dimensions extracted from "
                    "the package drawing",
                )
            )
    return findings
