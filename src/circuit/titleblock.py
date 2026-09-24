"""Inject or update the ``(title_block ...)`` element of a ``.kicad_sch`` file.

Konnect-authored schematics carry no title block, which leaves the sheet
frame's title/date/rev fields blank in printed output. This module edits the
schematic text in place so the fields are populated; it is a render-quality
fix only and never touches connectivity.
"""

from __future__ import annotations

import re
from pathlib import Path


class TitleBlockError(ValueError):
    """Raised when the schematic text cannot be edited safely."""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _list_end(text: str, start: int) -> int:
    """Return the index just past the ``)`` closing the ``(`` at ``start``."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise TitleBlockError("unbalanced parentheses in schematic text")


def inject_title_block(
    path: Path,
    *,
    title: str,
    date: str,
    rev: str,
    company: str | None = None,
) -> dict[str, str]:
    """Write ``title``/``date``/``rev`` (and ``company`` when given) into the
    schematic's title block, creating the element when absent.

    Returns the field values written, for logging."""
    text = path.read_text(encoding="utf-8")
    values = {"title": title, "date": date, "rev": rev}
    if company is not None:
        values["company"] = company

    match = re.search(r"\(\s*title_block\b", text)
    if match is not None:
        block_start = match.start()
        block_end = _list_end(text, block_start)
        block = text[block_start:block_end]
        for name, value in values.items():
            field_re = re.compile(r"\(\s*" + re.escape(name) + r"\b[^)]*\)")
            field_match = field_re.search(block)
            replacement = f'({name} "{_escape(value)}")'
            if field_match is not None:
                block = block[: field_match.start()] + replacement + block[field_match.end() :]
            else:
                insert_at = block.rfind(")")
                block = block[:insert_at] + "\t\t" + replacement + "\n\t" + block[insert_at:]
        text = text[:block_start] + block + text[block_end:]
    else:
        fields = "\n".join(f'\t\t({name} "{_escape(value)}")' for name, value in values.items())
        block = f"\t(title_block\n{fields}\n\t)\n"
        paper_match = re.search(r"\(\s*paper\b", text)
        if paper_match is not None:
            at = _list_end(text, paper_match.start())
            text = text[:at] + "\n" + block + text[at:]
        else:
            at = text.rfind(")")
            if at < 0:
                raise TitleBlockError("no root list found in schematic text")
            text = text[:at] + block + text[at:]

    path.write_text(text, encoding="utf-8")
    return values
