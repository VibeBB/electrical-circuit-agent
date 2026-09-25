"""Clamp out-of-bounds net labels back inside the sheet frame.

Authoring ops (Konnect or generated scripts) can place label ``at``
coordinates outside the paper bounds, which ``sch_lint`` reports as
``item_out_of_bounds`` errors. Only label items are moved: a label
outside the sheet cannot be attached to anything — wires and pins live
inside the sheet — so clamping changes rendering only, never
connectivity. Symbols, wires, and junctions are left untouched because
their positions are electrical, not cosmetic.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import TypedDict

from . import titleblock

_LABEL_RE = re.compile(r"\(\s*(label|global_label|hierarchical_label)\b")
_AT_RE = re.compile(r"\(\s*at\s+(-?[\d.]+)\s+(-?[\d.]+)")
_SIZE_RE = re.compile(r'"(A[0-5])"')

# Keep a clamped label visibly inside the frame rather than exactly on
# the borderline.
EDGE_MARGIN_MM = 2.54

Move = TypedDict("Move", {"item": str, "from": list[float], "to": list[float]})


class FitSheetError(ValueError):
    """Raised when the schematic cannot be clamped safely."""


def _sheet_size(text: str) -> tuple[float, float]:
    """Return ``(width, height)`` of the sheet's ``(paper ...)`` element."""
    match = re.search(r"\(\s*paper\b", text)
    block = text
    if match is not None:
        block = text[match.start() : titleblock.list_end(text, match.start())]
    name = _SIZE_RE.search(block)
    width, height = titleblock.PAPER_SIZES.get(
        name.group(1) if name else "A4", titleblock.PAPER_SIZES["A4"]
    )
    if re.search(r"\bportrait\b", block):
        return height, width
    return width, height


def clamp_labels(path: Path, margin: float = EDGE_MARGIN_MM) -> list[Move]:
    """Move every out-of-bounds label item to the nearest in-sheet point.

    Returns the moves applied (empty when the sheet is already clean) —
    each entry names the element kind and the old/new ``at`` position.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FitSheetError(f"could not read schematic {path}: {exc}") from exc
    width, height = _sheet_size(text)
    moves: list[Move] = []
    for match in _LABEL_RE.finditer(text):
        end = titleblock.list_end(text, match.start())
        block = text[match.start() : end]
        at = _AT_RE.search(block)
        if at is None:
            continue
        x, y = float(at.group(1)), float(at.group(2))
        nx = min(max(x, margin), width - margin)
        ny = min(max(y, margin), height - margin)
        if nx == x and ny == y:
            continue
        block = block[: at.start()] + f"(at {nx:.2f} {ny:.2f}" + block[at.end() :]
        text = text[: match.start()] + block + text[end:]
        moves.append(
            {
                "item": match.group(1),
                "from": [x, y],
                "to": [nx, ny],
            }
        )
    if moves:
        titleblock.atomic_write(path, text)
    return moves


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m circuit.fit_sheet",
        description="clamp out-of-bounds schematic labels inside the sheet",
    )
    parser.add_argument("schematic")
    parser.add_argument("--margin", type=float, default=EDGE_MARGIN_MM)
    args = parser.parse_args(argv)
    moves = clamp_labels(Path(args.schematic), margin=args.margin)
    print(json.dumps({"clamped": len(moves), "items": moves}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
