"""fit_sheet clamp + titleblock atomic-write checks."""

from pathlib import Path

import pytest

from circuit import fit_sheet, sch_lint, sexpr, titleblock

BASE = (
    "(kicad_sch\n"
    "\t(version 20250610)\n"
    '\t(generator "konnect")\n'
    '\t(paper "A4")\n'
    "\t(lib_symbols\n"
    "\t)\n"
    "{body}"
    "\t(sheet_instances\n"
    '\t\t(path "/"\n'
    '\t\t\t(page "1")\n'
    "\t\t)\n"
    "\t)\n"
    ")\n"
)

LABEL = '\t(label "{name}" (at {x} {y} {rot}))\n'
WIRE = "\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2})))\n"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sheet.kicad_sch"
    path.write_text(BASE.format(body=body), encoding="utf-8")
    return path


def test_out_of_bounds_label_is_clamped(tmp_path: Path) -> None:
    schematic = _write(tmp_path, LABEL.format(name="PWR", x=22.86, y=-25.40, rot=0))
    moves = fit_sheet.clamp_labels(schematic)
    assert len(moves) == 1
    assert moves[0]["item"] == "label"
    assert moves[0]["to"][1] == pytest.approx(fit_sheet.EDGE_MARGIN_MM)
    root = sexpr.parse_text(schematic.read_text(encoding="utf-8"))
    report = sch_lint.lint_schematic(schematic)
    assert all(f.type != "item_out_of_bounds" for f in report.findings)
    assert root[0] == "kicad_sch"


def test_in_bounds_label_and_wires_untouched(tmp_path: Path) -> None:
    body = LABEL.format(name="NET", x=100.0, y=80.0, rot=0) + WIRE.format(
        x1=0, y1=-10, x2=50, y2=-10
    )
    schematic = _write(tmp_path, body)
    before = schematic.read_text(encoding="utf-8")
    moves = fit_sheet.clamp_labels(schematic)
    assert moves == []
    # no rewrite happened — the wire outside the bounds is left alone
    assert schematic.read_text(encoding="utf-8") == before


def test_global_and_hierarchical_labels_clamped(tmp_path: Path) -> None:
    body = (
        '\t(global_label "5V" (at -40.0 100.0))\n'
        + '\t(hierarchical_label "IN" (at 400.0 250.0))\n'
    )
    schematic = _write(tmp_path, body)
    moves = fit_sheet.clamp_labels(schematic)
    assert len(moves) == 2
    assert moves[0]["to"][0] == pytest.approx(fit_sheet.EDGE_MARGIN_MM)
    # A4 landscape: 297x210 -> x clamps to 297 - margin
    assert moves[1]["to"][0] == pytest.approx(297.0 - fit_sheet.EDGE_MARGIN_MM)


def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    schematic = _write(tmp_path, LABEL.format(name="A", x=10, y=10, rot=0))
    titleblock.inject_title_block(schematic, title="t", date="d", rev="1")
    assert not list(tmp_path.glob("*.tmp"))
    assert "(title_block" in schematic.read_text(encoding="utf-8")


def test_inject_title_block_preserves_valid_sexpr(tmp_path: Path) -> None:
    schematic = _write(tmp_path, LABEL.format(name="A", x=10, y=10, rot=0))
    titleblock.inject_title_block(
        schematic, title="t", date="d", rev="1", paper="A3", comments=["a long " * 30]
    )
    sexpr.parse_text(schematic.read_text(encoding="utf-8"))
    report = sch_lint.lint_schematic(schematic)
    assert all(f.type != "item_out_of_bounds" for f in report.findings)
