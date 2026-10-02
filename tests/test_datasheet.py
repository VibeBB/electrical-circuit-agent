import hashlib
import json
import stat
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from circuit import datasheet as datasheet_module
from circuit.datasheet import (
    DatasheetError,
    PdfWord,
    extract_datasheet,
    load_extraction,
    page_tables,
    page_words,
)
from circuit.humanrequest import build_request


def _all_words_visible(
    _image_path: Path,
    words: Sequence[PdfWord],
    **_kwargs: object,
) -> tuple[list[PdfWord], list[PdfWord]]:
    return list(words), []


def _pdf(
    path: Path,
    pages: list[tuple[list[str], int]],
    *,
    page_size: tuple[int, int] = (200, 200),
    extra_commands: list[list[str]] | None = None,
) -> Path:
    page_ids = [3 + index * 2 for index in range(len(pages))]
    font_id = 3 + len(pages) * 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_ids)}] "
            f"/Count {len(pages)} >>"
        ).encode(),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, (lines, vector_count) in enumerate(pages):
        page_id = page_ids[index]
        content_id = page_id + 1
        text_commands: list[str] = []
        y = 170
        for text in lines:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if text in ("1,2,3", "SW"):
                x, text_y = (25, 76) if text == "1,2,3" else (65, 76)
            else:
                x, text_y = 20, y
            text_commands.append(f"BT /F1 10 Tf {x} {text_y} Td ({escaped}) Tj ET")
            y -= 14
        if "1,2,3" in lines and "SW" in lines:
            text_commands.extend(
                [
                    "20 70 80 18 re S",
                    "60 70 m 60 88 l S",
                    "20 70 m 100 70 l S",
                    "20 88 m 100 88 l S",
                ]
            )
        if extra_commands is not None:
            text_commands.extend(extra_commands[index])
        text_commands.extend(
            f"{number % 80 + 110} 10 m {number % 80 + 111} 11 l S" for number in range(vector_count)
        )
        stream = "\n".join(text_commands).encode()
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_size[0]} {page_size[1]}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    data = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id, body in sorted(objects.items()):
        offsets[object_id] = len(data)
        data.extend(f"{object_id} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(data)
    size = max(objects) + 1
    data.extend(f"xref\n0 {size}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for object_id in range(1, size):
        data.extend(f"{offsets[object_id]:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(data)
    return path


def _stub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rasterizer = _stub(
        tmp_path / "pdftoppm",
        '#!/bin/bash\nprefix="${@: -1}"\nfirst=1\nwhile [ $# -gt 0 ]; do\n'
        'case "$1" in -f) shift; first="$1";; esac\nshift\ndone\n'
        'printf "fake png" > "$prefix-$first.png"\n',
    )
    poppler = _stub(
        tmp_path / "pdftotext",
        '#!/bin/bash\nif [ "$1" = "-v" ]; then echo "pdftotext version 24.02.0" >&2; exit 0; fi\n'
        'mode="${CIRCUIT_TEST_POPPLER_MODE:-ok}"\n'
        'if [ "$mode" = "error" ]; then echo "controlled failure" >&2; exit 2; fi\n'
        'output="${@: -1}"\n'
        'if [ "$mode" = "empty" ]; then printf \'<html xmlns="http://www.w3.org/1999/xhtml"/>\' '
        '> "$output"; exit 0; fi\n'
        'if [ "$mode" = "reverse" ]; then words="SW 1,2,3 0.07 1.68"; '
        'else words="1.68 0.07 1,2,3 SW"; fi\n'
        'printf \'<html xmlns="http://www.w3.org/1999/xhtml"><body>\' > "$output"\n'
        'for word in $words; do printf \'<word xMin="20" yMin="20" xMax="40" yMax="30">%s</word>\' '
        '"$word" >> "$output"; done\n'
        "printf '</body></html>' >> \"$output\"\n",
    )
    tesseract = _stub(
        tmp_path / "tesseract",
        '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "tesseract 5.3.4"; exit 0; fi\n'
        'printf "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\t'
        'left\\ttop\\twidth\\theight\\tconf\\ttext\\n"\n'
        'printf "5\\t1\\t1\\t1\\t1\\t1\\t30\\t60\\t40\\t20\\t90\\tOCRWORD\\n"\n',
    )
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", str(rasterizer))
    monkeypatch.setenv("CIRCUIT_PDFTOTEXT", str(poppler))
    monkeypatch.setenv("CIRCUIT_TESSERACT", str(tesseract))


def _datasheet_request() -> Any:
    return build_request(
        kind="datasheet_acquisition",
        subject={"manufacturer": "Example", "mpn": "ABC123", "revision": "Rev B"},
        reason="A matching datasheet is needed.",
        evidence=[
            {
                "kind": "note",
                "ref": "attempts",
                "summary": "The manufacturer datasheet was not found.",
            }
        ],
        known=["The requested part is ABC123."],
        unknown=["The package drawing remains unverified."],
        agent_assessment=(
            "A datasheet matching the requested manufacturer part number and package evidence "
            "is needed before the library can proceed. The package drawing, pinout, orderable "
            "variants, and mechanical dimensions must be checked against the received source. "
            "Until those claims are supported by evidence, the library artifacts remain blocked "
            "and no release decision is justified."
        ),
        recommendation="Provide the requested datasheet.",
        recommendation_rationale="The source evidence is required to verify the package.",
        alternatives=[
            {
                "option": "Provide the requested datasheet.",
                "risks": ["The library remains blocked until the source is checked."],
            },
            {
                "option": "Mark it unavailable.",
                "risks": ["Alternative evidence will be needed."],
            },
        ],
        recommended=0,
        details={
            "kind": "datasheet_acquisition",
            "failure_reason": "not_found",
            "requested_revision": "Rev B",
            "required_sections": [
                "package_drawing",
                "pinout",
                "pin_table",
                "land_pattern",
                "orderable_table",
            ],
            "attempted_sources": ["manufacturer website"],
            "optional_cad_requested": False,
        },
    )


def _stub_received_extraction(
    monkeypatch: pytest.MonkeyPatch,
    lane_text: dict[str, str],
    extraction_dirs: list[Path],
) -> None:
    extraction = SimpleNamespace(pages=[SimpleNamespace(page=1)])

    def extract(_pdf: Path, output_dir: Path, **_kwargs: object) -> Any:
        extraction_dirs.append(output_dir)
        return extraction

    def words(
        _extraction: Any,
        _directory: Path,
        _page: int,
        *,
        lanes: Sequence[str],
    ) -> list[PdfWord]:
        return [
            PdfWord(text=token, x0=0, top=0, x1=1, bottom=1)
            for token in lane_text[lanes[0]].split()
        ]

    monkeypatch.setattr(datasheet_module, "extract_datasheet", extract)
    monkeypatch.setattr(datasheet_module, "page_words", words)


_VALID_RECEIVED_TEXT = (
    "ABC123 ordering information Rev B package outline top view pin configuration "
    "pin number pin name recommended land pattern"
)


def test_check_received_requires_mpn_revision_and_sections_in_both_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "received.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    output_dirs: list[Path] = []
    _stub_received_extraction(
        monkeypatch,
        {
            "poppler": "ordering information Rev A package outline top view pin configuration "
            "pin number pin name recommended land pattern",
            "pdfplumber": _VALID_RECEIVED_TEXT,
        },
        output_dirs,
    )

    findings = datasheet_module.check_received(pdf, _datasheet_request())

    assert {finding.code for finding in findings} == {
        "datasheet_mpn_mismatch",
        "datasheet_revision_mismatch",
    }
    assert len(output_dirs) == 1
    assert findings[0].severity == "error"


def test_check_received_accepts_matching_evidence_in_both_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "received.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    output_dirs: list[Path] = []
    _stub_received_extraction(
        monkeypatch,
        {"poppler": _VALID_RECEIVED_TEXT, "pdfplumber": _VALID_RECEIVED_TEXT},
        output_dirs,
    )

    assert datasheet_module.check_received(pdf, _datasheet_request()) == []


def test_check_received_reports_missing_sections_and_uses_private_temp_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    pdf = project / ".confidential" / "received.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    output_dirs: list[Path] = []
    text_without_package_drawing = (
        "ABC123 ordering information Rev B top view pin configuration pin number pin name "
        "recommended land pattern"
    )
    _stub_received_extraction(
        monkeypatch,
        {
            "poppler": text_without_package_drawing,
            "pdfplumber": text_without_package_drawing,
        },
        output_dirs,
    )

    findings = datasheet_module.check_received(pdf, _datasheet_request())

    assert [finding.code for finding in findings] == ["datasheet_section_missing:package_drawing"]
    assert len(output_dirs) == 1
    assert output_dirs[0].resolve().is_relative_to((project / ".confidential").resolve())
    assert (project / ".confidential" / ".gitignore").is_file()


def test_extract_datasheet_writes_manifest_and_both_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path, monkeypatch)
    pdf_path = _pdf(
        tmp_path / "parts.pdf",
        [(["1.68", "0.07", "1,2,3", "SW"], 501)],
    )
    out_dir = tmp_path / "extract"
    extraction = extract_datasheet(pdf_path, out_dir, dpi=300)
    page = extraction.pages[0]
    assert extraction.artifact_kind == "circuit_datasheet_extraction"
    assert extraction.pdf_sha256 == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    assert extraction.tools["pdfplumber"] == "0.11.10"
    assert page.vector_objects >= 500
    assert page.drawing_page is True
    assert page.text_layer is True
    assert page.order_similarity is not None
    assert [lane.lane for lane in page.lanes] == ["poppler", "pdfplumber"]
    assert (out_dir / "extraction.json").is_file()
    assert (out_dir / "page-001.png").read_bytes() == b"fake png"
    assert page.table_count >= 1
    assert page_tables(extraction, out_dir, 1)
    assert page_words(extraction, out_dir, 1)
    assert load_extraction(out_dir / "extraction.json") == extraction


def test_extract_datasheet_records_lane_unavailable_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path, monkeypatch)
    pdf_path = _pdf(tmp_path / "parts.pdf", [(["1.68"], 0)])
    monkeypatch.setenv("CIRCUIT_PDFTOTEXT", str(tmp_path / "missing-pdftotext"))
    extraction = extract_datasheet(pdf_path, tmp_path / "unavailable")
    assert extraction.pages[0].lanes[0].status == "unavailable"
    assert "poppler-utils" in extraction.pages[0].lanes[0].detail
    _tools(tmp_path, monkeypatch)
    monkeypatch.setenv("CIRCUIT_TEST_POPPLER_MODE", "error")
    extraction = extract_datasheet(pdf_path, tmp_path / "error")
    assert extraction.pages[0].lanes[0].status == "error"
    assert "controlled failure" in extraction.pages[0].lanes[0].detail


def test_extract_datasheet_uses_ocr_for_page_without_text_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path, monkeypatch)
    monkeypatch.setenv("CIRCUIT_TEST_POPPLER_MODE", "empty")
    pdf_path = _pdf(tmp_path / "image-only.pdf", [([], 0)])
    extraction = extract_datasheet(pdf_path, tmp_path / "ocr", dpi=300)
    page = extraction.pages[0]
    assert page.text_layer is False
    assert [lane.lane for lane in page.lanes] == ["poppler", "pdfplumber", "ocr"]
    assert page.lanes[2].status == "ok"
    words = page_words(extraction, tmp_path / "ocr", 1, lanes=("ocr",))
    assert words[0].text == "OCRWORD"
    assert words[0].x0 == pytest.approx(7.2)
    assert words[0].top == pytest.approx(14.4)


def test_extract_datasheet_records_order_divergence_and_ocr_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path, monkeypatch)
    monkeypatch.setenv("CIRCUIT_TEST_POPPLER_MODE", "reverse")
    pdf_path = _pdf(tmp_path / "parts.pdf", [(["1.68", "0.07", "1,2,3", "SW"], 0)])
    extraction = extract_datasheet(pdf_path, tmp_path / "reverse")
    assert extraction.pages[0].order_similarity is not None
    assert extraction.pages[0].order_similarity < 1
    assert all(lane.lane != "ocr" for lane in extraction.pages[0].lanes)

    monkeypatch.setenv("CIRCUIT_TEST_POPPLER_MODE", "empty")
    monkeypatch.setenv("CIRCUIT_TESSERACT", str(tmp_path / "missing-tesseract"))
    image_pdf = _pdf(tmp_path / "image.pdf", [([], 0)])
    extraction = extract_datasheet(image_pdf, tmp_path / "ocr-missing")
    assert extraction.pages[0].lanes[-1].status == "unavailable"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Package Outline", True),
        ("mEcHaNiCaL dAtA", True),
        ("Scale 3.6", True),
        ("scalable instructions", False),
    ],
)
def test_drawing_page_detection_uses_visible_phrases_and_scale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    line: str,
    expected: bool,
) -> None:
    _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(datasheet_module, "words_by_ink", _all_words_visible)
    pdf_path = _pdf(tmp_path / "drawing.pdf", [([line], 0)])
    extraction = extract_datasheet(pdf_path, tmp_path / "drawing")
    assert extraction.pages[0].drawing_page is expected


def test_extract_datasheet_fails_closed_on_invalid_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path, monkeypatch)
    with pytest.raises(DatasheetError, match="missing"):
        extract_datasheet(tmp_path / "missing.pdf", tmp_path / "out")
    not_pdf = tmp_path / "notes.pdf"
    not_pdf.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(DatasheetError, match="not a PDF"):
        extract_datasheet(not_pdf, tmp_path / "out")
    pdf_path = _pdf(tmp_path / "parts.pdf", [(["1.68"], 0)])
    with pytest.raises(DatasheetError, match="outside"):
        extract_datasheet(pdf_path, tmp_path / "out", pages=[2])
    with pytest.raises(DatasheetError, match="dpi"):
        extract_datasheet(pdf_path, tmp_path / "out", dpi=71)

    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.4\ninvalid content")
    with pytest.raises(
        DatasheetError,
        match=r"(could not (open|read) PDF|PDF contains no pages)",
    ):
        extract_datasheet(corrupt_pdf, tmp_path / "corrupt")


def test_extract_datasheet_rasterizer_failure_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = _pdf(tmp_path / "parts.pdf", [([], 0)])
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", str(tmp_path / "missing-pdftoppm"))
    with pytest.raises(DatasheetError, match="rasterize page"):
        extract_datasheet(pdf_path, tmp_path / "out")


def test_extraction_loading_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"artifact_kind": "wrong"}), encoding="utf-8")
    with pytest.raises(DatasheetError, match="could not load"):
        load_extraction(path)


def test_rotated_character_words_rebuild_both_reading_directions() -> None:
    private_api: Any = datasheet_module

    def char(text: str, top: float, *, x: float = 10.0, b: float = -1.0) -> dict[str, object]:
        return {
            "text": text,
            "x0": x - 1,
            "x1": x + 1,
            "top": top,
            "bottom": top + 10,
            "size": 10,
            "matrix": (0, b, -b, 0, x, top),
        }

    ttb = [char(letter, index * 10) for index, letter in enumerate("PGND")]
    btt = [char(letter, index * 10, x=30, b=1) for index, letter in enumerate("DNGP")]

    assert [word.text for word in private_api._rotated_char_words(ttb)] == ["PGND"]
    assert [word.text for word in private_api._rotated_char_words(btt)] == ["PGND"]


def test_rotated_character_words_separate_columns_and_whitespace() -> None:
    private_api: Any = datasheet_module

    def char(text: str, top: float, *, x: float = 10.0) -> dict[str, object]:
        return {
            "text": text,
            "x0": x - 1,
            "x1": x + 1,
            "top": top,
            "bottom": top + 10,
            "size": 10,
            "matrix": (0, -1, 1, 0, x, top),
        }

    chars = [
        *(char(letter, index * 10) for index, letter in enumerate("PG")),
        char(" ", 20),
        *(char(letter, index * 10 + 30) for index, letter in enumerate("ND")),
        *(char(letter, index * 10, x=30) for index, letter in enumerate("VOS")),
    ]

    words = private_api._rotated_char_words(chars)
    assert {word.text for word in words} == {"PG", "ND", "VOS"}
    assert len(words) == 3
