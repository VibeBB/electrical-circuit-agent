"""ISO 7200 drawing sheet (``.kicad_wks``) and project text variables.

KiCad's default sheet prints Company/Title/Rev/Date/Comment fields but has
no slot for the approver, the document status, or the source brief. This
module emits a drawing sheet whose title block follows ISO 7200:2004 and
whose values come from project text variables, so the ``.kicad_sch`` stays
untouched and a re-run only rewrites the project file.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from . import __version__
from .brief import DesignBrief

BLANK = "\u2014"
TITLE_BLOCK_WIDTH_MM = 180.0
_ROW_MM = 7.0
_ROWS = 6
_INSET_MM = 2.0

# (variable, label, left_mm, right_mm, top_row, bottom_row, value_size_mm)
# Columns are measured from the title block's left edge; row 0 is the top.
CELLS: tuple[tuple[str, str, float, float, int, int, float], ...] = (
    ("VIBEBB_GENERATOR", "Generator", 0.0, 60.0, 0, 1, 1.4),
    ("VIBEBB_BRIEF_SHA256", "Brief sha256", 60.0, 125.0, 0, 1, 1.4),
    ("FILENAME", "File", 125.0, 180.0, 0, 1, 1.4),
    ("VIBEBB_DEPT", "Responsible dept.", 0.0, 40.0, 1, 2, 1.6),
    ("VIBEBB_TECHREF", "Technical reference", 40.0, 85.0, 1, 2, 1.6),
    ("VIBEBB_CREATED_BY", "Created by", 85.0, 130.0, 1, 2, 1.6),
    ("VIBEBB_APPROVED_BY", "Approved by", 130.0, 180.0, 1, 2, 1.6),
    ("VIBEBB_OWNER", "Legal owner", 0.0, 40.0, 2, 6, 2.0),
    ("VIBEBB_DOCTYPE", "Document type", 40.0, 95.0, 2, 3, 1.6),
    ("VIBEBB_CLASSIFICATION", "Classification/key words", 95.0, 135.0, 2, 3, 1.6),
    ("VIBEBB_STATUS", "Document status", 135.0, 180.0, 2, 3, 1.6),
    ("TITLE", "Title, Supplementary title", 40.0, 110.0, 3, 5, 2.5),
    ("VIBEBB_ID", "Identification number", 110.0, 180.0, 3, 5, 2.5),
    ("SHEETPATH", "Sheet path", 40.0, 110.0, 5, 6, 1.6),
    ("REVISION", "Rev.", 110.0, 125.0, 5, 6, 1.6),
    ("VIBEBB_DATE", "Date of issue", 125.0, 150.0, 5, 6, 1.6),
    ("VIBEBB_LANGUAGE", "Lang.", 150.0, 162.0, 5, 6, 1.6),
    ("#", "Sheet", 162.0, 180.0, 5, 6, 1.6),
)


def _x(left_mm: float) -> float:
    return _INSET_MM + TITLE_BLOCK_WIDTH_MM - left_mm


def _y(row: int) -> float:
    return _INSET_MM + _ROW_MM * (_ROWS - row)


def _fmt(value: float) -> str:
    return f"{value:g}"


def _frame() -> list[str]:
    return [
        '(rect (name "") (start 0 0 ltcorner) (end 0 0) (repeat 2) (incrx 2) (incry 2))',
        '(line (name "") (start 50 2 ltcorner) (end 50 0 ltcorner) (repeat 30) (incrx 50))',
        '(tbtext "1" (name "") (pos 25 1 ltcorner) (font (size 1.3 1.3)) (repeat 100) (incrx 50))',
        '(line (name "") (start 50 2 lbcorner) (end 50 0 lbcorner) (repeat 30) (incrx 50))',
        '(tbtext "1" (name "") (pos 25 1 lbcorner) (font (size 1.3 1.3)) (repeat 100) (incrx 50))',
        '(line (name "") (start 0 50 ltcorner) (end 2 50 ltcorner) (repeat 30) (incry 50))',
        '(tbtext "A" (name "") (pos 1 25 ltcorner) (font (size 1.3 1.3)) (justify center)'
        " (repeat 100) (incry 50))",
        '(line (name "") (start 0 50 rtcorner) (end 2 50 rtcorner) (repeat 30) (incry 50))',
        '(tbtext "A" (name "") (pos 1 25 rtcorner) (font (size 1.3 1.3)) (justify center)'
        " (repeat 100) (incry 50))",
        '(tbtext "${PAPER}" (name "") (pos 1 6) (font (size 1.3 1.3)) (justify center)'
        ' (rotate 90) (comment "paper size lives in the frame, not the title block"))',
    ]


def _cell(cell: tuple[str, str, float, float, int, int, float]) -> list[str]:
    variable, label, left, right, top, bottom, size = cell
    x0, x1, y0, y1 = _x(left), _x(right), _y(top), _y(bottom)
    value = "${#}/${##}" if variable == "#" else "${" + variable + "}"
    value_y = y1 + (y0 - y1 - 1.6) / 2
    items = [
        f'(rect (name "") (start {_fmt(x0)} {_fmt(y0)}) (end {_fmt(x1)} {_fmt(y1)}))',
        f'(tbtext "{label}" (name "") (pos {_fmt(x0 - 0.8)} {_fmt(y0 - 1.3)}) (font (size 1 1)))',
    ]
    if variable == "TITLE":
        items.append(
            f'(tbtext "${{TITLE}}" (name "") (pos {_fmt(x0 - 0.8)} {_fmt(y0 - 5.4)})'
            f" (font (size {size} {size}) bold) (maxlen {_fmt(right - left - 2)}))"
        )
        items.append(
            f'(tbtext "${{VIBEBB_SUPPLEMENTARY}}" (name "") (pos {_fmt(x0 - 0.8)}'
            f" {_fmt(y1 + 2.2)}) (font (size 1.6 1.6)) (maxlen {_fmt(right - left - 2)}))"
        )
        return items
    bold = " bold" if variable == "VIBEBB_ID" else ""
    items.append(
        f'(tbtext "{value}" (name "") (pos {_fmt(x0 - 0.8)} {_fmt(value_y)})'
        f" (font (size {size} {size}){bold}) (maxlen {_fmt(right - left - 2)}))"
    )
    return items


def sheet_text() -> str:
    """Deterministic ``.kicad_wks`` text for the ISO 7200 sheet."""
    lines = [
        "(kicad_wks (version 20210606) (generator circuit)",
        "(setup (textsize 1.5 1.5)(linewidth 0.15)(textlinewidth 0.15)",
        "(left_margin 10)(right_margin 10)(top_margin 10)(bottom_margin 10))",
        *_frame(),
    ]
    for cell in CELLS:
        lines.extend(_cell(cell))
    lines.append(")")
    return "\n".join(lines) + "\n"


def variables(brief: DesignBrief, *, brief_sha256: str) -> dict[str, str]:
    """Project text variables the sheet prints; unset fields print a dash."""
    info = brief.drawing
    return {
        "VIBEBB_GENERATOR": f"circuit-agent/{__version__}",
        "VIBEBB_BRIEF_SHA256": brief_sha256[:16],
        "VIBEBB_DEPT": info.responsible_dept or BLANK,
        "VIBEBB_TECHREF": info.technical_reference or BLANK,
        "VIBEBB_CREATED_BY": info.created_by or BLANK,
        "VIBEBB_APPROVED_BY": info.approved_by or BLANK,
        "VIBEBB_OWNER": info.legal_owner or BLANK,
        "VIBEBB_DOCTYPE": "Circuit diagram",
        "VIBEBB_CLASSIFICATION": info.classification or BLANK,
        "VIBEBB_STATUS": info.status,
        "VIBEBB_ID": info.identification(brief.name),
        "VIBEBB_SUPPLEMENTARY": info.supplementary_title or "",
        "VIBEBB_DATE": info.date_of_issue or BLANK,
        "VIBEBB_LANGUAGE": info.language,
    }


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def apply(project: Path, *, text_variables: dict[str, str]) -> Path:
    """Write ``<stem>.kicad_wks`` beside ``project`` and point the project at it.

    Only ``schematic.page_layout_descr_file`` and the ``VIBEBB_*`` text
    variables are touched; every other project setting is preserved. A
    missing or unparsable project fails closed.
    """
    try:
        data: Any = json.loads(project.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read KiCad project {project}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"KiCad project {project} is not a JSON object")
    settings = cast(dict[str, Any], data)
    schematic = settings.setdefault("schematic", {})
    if not isinstance(schematic, dict):
        raise ValueError(f"KiCad project {project} has a non-object 'schematic'")
    sheet = project.with_suffix(".kicad_wks")
    _atomic_write(sheet, sheet_text())
    cast(dict[str, Any], schematic)["page_layout_descr_file"] = sheet.name
    merged: dict[str, Any] = {}
    existing = settings.get("text_variables")
    if isinstance(existing, dict):
        for key, value in cast(dict[str, Any], existing).items():
            if not key.startswith("VIBEBB_"):
                merged[key] = value
    merged.update(text_variables)
    settings["text_variables"] = dict(sorted(merged.items()))
    _atomic_write(project, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return sheet
