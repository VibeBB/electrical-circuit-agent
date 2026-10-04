from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from circuit import sexpr

_OUTPUT_DIR = Path("tests/fixtures/kicad_connplace_oracle/gui_flipped")
_BOARD_SUPPORT = {"version", "generator", "general", "paper", "layers", "setup"}


def _children(node: list[sexpr.SExpr], tag: str) -> list[list[sexpr.SExpr]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == tag]


def _reference(footprint: list[sexpr.SExpr]) -> str | None:
    for prop in _children(footprint, "property"):
        if len(prop) >= 3 and str(prop[1]) == "Reference":
            return str(prop[2])
    return None


def _pretty_node(node: list[sexpr.SExpr], indent: int) -> list[str]:
    prefix = " " * indent + "(" + sexpr.serialize(node[0])
    lines = [prefix]
    nested = False
    for child in node[1:]:
        if isinstance(child, list):
            lines.append(" " * (indent + 2) + sexpr.serialize(child))
            nested = True
        elif nested:
            lines.append(" " * (indent + 2) + sexpr.serialize(child))
        else:
            lines[0] += " " + sexpr.serialize(child)
    if nested:
        lines.append(" " * indent + ")")
    else:
        lines[0] += ")"
    return lines


def _footprint(board: list[sexpr.SExpr], reference: str) -> list[sexpr.SExpr]:
    footprints = [node for node in _children(board, "footprint") if _reference(node) == reference]
    if len(footprints) != 1:
        raise ValueError(f"expected one footprint with reference {reference!r}")
    footprint = footprints[0]
    layers = _children(footprint, "layer")
    if not layers or str(layers[0][1]) != "B.Cu":
        raise ValueError(f"footprint {reference!r} is not on B.Cu")
    return footprint


def _minimal_board(source: Path, reference: str) -> str:
    root = sexpr.parse_text(source.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_pcb":
        raise ValueError(f"{source} is not a KiCad PCB file")
    footprint = _footprint(root, reference)
    net_ids = {
        str(net[1])
        for pad in _children(footprint, "pad")
        for net in _children(pad, "net")
        if len(net) > 1
    }
    entries: list[sexpr.SExpr] = []
    for node in root[1:]:
        if not isinstance(node, list) or not node:
            continue
        if (
            node[0] in _BOARD_SUPPORT
            or (node[0] == "net" and len(node) > 1 and str(node[1]) in net_ids)
            or (node[0] == "footprint" and node is footprint)
        ):
            entries.append(node)
    lines = ["(kicad_pcb"]
    for entry in entries:
        if not isinstance(entry, list):
            raise ValueError("unexpected scalar at the board root")
        lines.extend(_pretty_node(entry, 2))
    lines.append(")")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract minimal boards containing KiCad-saved bottom-side demo footprints."
    )
    parser.add_argument("--video-board", type=Path, required=True)
    parser.add_argument("--royalblue-board", type=Path, required=True)
    parser.add_argument("--soic-footprint", type=Path, required=True)
    parser.add_argument("--tag-connect-footprint", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=_OUTPUT_DIR)
    return parser


def main() -> int:
    args = _parser().parse_args()
    outputs = (
        (args.video_board, "U3", "video_U3.kicad_pcb"),
        (args.royalblue_board, "J8", "royalblue_J8.kicad_pcb"),
    )
    for source, reference, name in outputs:
        destination = args.output_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_minimal_board(source, reference), encoding="utf-8")
    shutil.copyfile(
        args.soic_footprint,
        args.output_dir / "SOIC-20W_7.5x12.8mm_P1.27mm.kicad_mod",
    )
    shutil.copyfile(
        args.tag_connect_footprint,
        args.output_dir / "Tag-Connect_TC2030-IDC-NL_2x03_P1.27mm_Vertical.kicad_mod",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
