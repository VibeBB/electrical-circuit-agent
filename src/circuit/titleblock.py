"""Inject or update the ``(title_block ...)`` element of a ``.kicad_sch`` file.

Konnect-authored schematics carry no title block, which leaves the sheet
frame's title/date/rev fields blank in printed output. This module edits the
schematic text in place so the fields are populated; it is a render-quality
fix only and never touches connectivity.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
import textwrap
from pathlib import Path

from .sexpr import SExprError, parse_text


class TitleBlockError(ValueError):
    """Raised when the schematic text cannot be edited safely."""


# KiCad prints each (comment N ...) field as one line beside the title
# block; a single over-long line runs past the sheet frame. Wrap comments
# so every printed line stays inside the frame.
_COMMENT_LINE_MAX = 50

# Sheet names understood by the ``paper`` element (landscape sizes).
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def list_end(text: str, start: int) -> int:
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


def _wrap_comments(comments: list[str]) -> list[str]:
    """Split each comment into lines that fit inside the sheet frame.

    Comments that already fit are kept verbatim; only over-long comments
    are re-flowed into consecutive numbered fields."""
    wrapped: list[str] = []
    for comment in comments:
        if len(comment) <= _COMMENT_LINE_MAX:
            wrapped.append(comment)
            continue
        for line in comment.splitlines():
            line = line.strip()
            if not line:
                continue
            wrapped.extend(
                textwrap.wrap(
                    line,
                    width=_COMMENT_LINE_MAX,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
            )
    return wrapped


def paper_for_part_count(
    count: int,
    *,
    x_step: float = 38.1,
    y_step: float = 38.1,
    columns: int = 4,
    origin: float = 50.8,
    margin: float = 40.0,
) -> str:
    """Return the smallest landscape sheet fitting the diagonal placement
    grid used by the deterministic authoring flow (``count`` parts laid out
    at ``x = origin + x_step*i``, ``y = origin + y_step*(i//columns)``)."""
    if count <= 0:
        return "A4"
    max_x = origin + x_step * (count - 1) + margin
    max_y = origin + y_step * math.ceil(count / columns) + margin
    for name, (width, height) in PAPER_SIZES.items():
        if max_x <= width and max_y <= height:
            return name
    return "A0"


def _set_paper(text: str, size: str) -> str:
    """Replace or insert the top-level ``(paper ...)`` element."""
    if size not in PAPER_SIZES:
        raise TitleBlockError(f"unknown paper size {size!r}")
    element = f'\t(paper "{size}")\n'
    match = re.search(r"\(\s*paper\b", text)
    if match is not None:
        end = list_end(text, match.start())
        return text[: match.start()] + element.rstrip("\n") + text[end:]
    version = re.search(r"\(\s*version\b", text)
    if version is not None:
        at = list_end(text, version.start())
        return text[:at] + "\n" + element.rstrip("\n") + text[at:]
    at = text.rfind(")")
    if at < 0:
        raise TitleBlockError("no root list found in schematic text")
    return text[:at] + element + ")" + text[at + 1 :]


def atomic_write(path: Path, text: str) -> None:
    """Validate then atomically replace the schematic file.

    The result must parse as one complete root s-expression before it
    reaches disk, and the write itself is a temp-file + rename so a
    concurrent reader (or a crash mid-write) can never observe a
    truncated file. A ``.kicad_sch`` is edited in place by multiple
    authoring ops, so partial writes are not acceptable here.
    """
    try:
        parse_text(text)
    except SExprError as exc:
        raise TitleBlockError(
            f"refusing to write malformed schematic to {path}: {exc}"
        ) from exc
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_paper_size(path: Path, size: str) -> str:
    """Set the schematic's ``(paper ...)`` element to ``size`` (e.g. "A3")."""
    text = path.read_text(encoding="utf-8")
    text = _set_paper(text, size)
    atomic_write(path, text)
    return size


def inject_title_block(
    path: Path,
    *,
    title: str,
    date: str,
    rev: str,
    company: str | None = None,
    comments: list[str] | None = None,
    paper: str | None = None,
) -> dict[str, str]:
    """Write ``title``/``date``/``rev`` (and ``company`` when given) into the
    schematic's title block, creating the element when absent.

    ``comments`` become numbered ``(comment N "...")`` fields — the design
    intent/documentation lines printed beside the title block. Each comment
    is wrapped at ``_COMMENT_LINE_MAX`` characters into consecutive comment
    fields so no printed line overruns the sheet frame. ``paper`` sets the
    sheet's ``(paper ...)`` size when given.

    Returns the field values written, for logging."""
    text = path.read_text(encoding="utf-8")
    values = {"title": title, "date": date, "rev": rev}
    if company is not None:
        values["company"] = company
    if paper is not None:
        text = _set_paper(text, paper)
    comment_items = [
        (f"comment_{index + 1}", comment)
        for index, comment in enumerate(_wrap_comments(comments or []))
    ]
    for key, comment in comment_items:
        values[key] = comment

    match = re.search(r"\(\s*title_block\b", text)
    if match is not None:
        block_start = match.start()
        block_end = list_end(text, block_start)
        block = text[block_start:block_end]
        for name, value in values.items():
            if name.startswith("comment_"):
                index = name.removeprefix("comment_")
                field_re = re.compile(r"\(\s*comment\s+" + re.escape(index) + r"\b[^)]*\)")
                replacement = f'(comment {index} "{_escape(value)}")'
            else:
                field_re = re.compile(r"\(\s*" + re.escape(name) + r"\b[^)]*\)")
                replacement = f'({name} "{_escape(value)}")'
            field_match = field_re.search(block)
            if field_match is not None:
                block = block[: field_match.start()] + replacement + block[field_match.end() :]
            else:
                insert_at = block.rfind(")")
                block = block[:insert_at] + "\t\t" + replacement + "\n\t" + block[insert_at:]
        text = text[:block_start] + block + text[block_end:]
    else:
        lines = [
            f'\t\t({name} "{_escape(value)}")'
            for name, value in values.items()
            if not name.startswith("comment_")
        ]
        lines.extend(
            f'\t\t(comment {index} "{_escape(comment)}")'
            for index, (_, comment) in enumerate(comment_items, start=1)
        )
        block = "\t(title_block\n" + "\n".join(lines) + "\n\t)\n"
        paper_match = re.search(r"\(\s*paper\b", text)
        if paper_match is not None:
            at = list_end(text, paper_match.start())
            text = text[:at] + "\n" + block + text[at:]
        else:
            at = text.rfind(")")
            if at < 0:
                raise TitleBlockError("no root list found in schematic text")
            text = text[:at] + block + text[at:]

    atomic_write(path, text)
    return values
