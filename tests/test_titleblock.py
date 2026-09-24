"""Title-block injection into .kicad_sch text."""

from pathlib import Path

import pytest

from circuit import sch_lint, sexpr, titleblock

KONNECT_STYLE = (
    "(kicad_sch\n"
    "\t(version 20250610)\n"
    '\t(generator "konnect")\n'
    '\t(uuid "0f79894c-3d5a-4192-b29f-bbbce852044c")\n'
    '\t(paper "A4")\n'
    "\t(lib_symbols\n"
    "\t)\n"
    "\t(sheet_instances\n"
    '\t\t(path "/"\n'
    '\t\t\t(page "1")\n'
    "\t\t)\n"
    "\t)\n"
    ")\n"
)

EMPTY_BLOCK = (
    "(kicad_sch\n"
    "\t(version 20250610)\n"
    '\t(uuid "abc")\n'
    '\t(paper "A4")\n'
    "\t(title_block\n"
    "\t\t(title\n"
    '\t\t\t"")\n'
    '\t\t(date "")\n'
    "\t)\n"
    "\t(sheet_instances\n"
    "\t)\n"
    ")\n"
)


def test_inject_creates_missing_title_block(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text(KONNECT_STYLE, encoding="utf-8")
    fields = titleblock.inject_title_block(path, title="led_loop", date="2026-09-24", rev="1")
    assert fields == {"title": "led_loop", "date": "2026-09-24", "rev": "1"}
    root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    block = [n for n in root if isinstance(n, list) and n and n[0] == "title_block"]
    assert len(block) == 1
    parsed = {f[0]: f[1] for f in block[0][1:]}
    assert parsed == fields


def test_inject_updates_existing_fields_and_keeps_text_valid(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text(EMPTY_BLOCK, encoding="utf-8")
    titleblock.inject_title_block(
        path, title='demo "board"', date="2026-01-01", rev="2", company="VibeBB"
    )
    text = path.read_text(encoding="utf-8")
    root = sexpr.parse_text(text)
    block = [n for n in root if isinstance(n, list) and n and n[0] == "title_block"]
    assert len(block) == 1
    parsed = {f[0]: f[1] for f in block[0][1:]}
    assert parsed == {
        "title": 'demo "board"',
        "date": "2026-01-01",
        "rev": "2",
        "company": "VibeBB",
    }
    # sheet_instances survived the edit
    assert any(isinstance(n, list) and n and n[0] == "sheet_instances" for n in root)


def test_inject_inserts_without_paper_element(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text("(kicad_sch\n\t(version 1)\n)\n", encoding="utf-8")
    titleblock.inject_title_block(path, title="t", date="d", rev="r")
    root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    assert any(isinstance(n, list) and n and n[0] == "title_block" for n in root)


def test_injected_block_clears_lint_warning(tmp_path: Path) -> None:
    body = KONNECT_STYLE.replace("(kicad_sch", "(kicad_sch", 1)
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    before = sch_lint.lint_file(sch)
    assert any(f.type == "title_block_incomplete" for f in before.findings)
    titleblock.inject_title_block(sch, title="t", date="d", rev="r")
    after = sch_lint.lint_file(sch)
    assert not any(f.type == "title_block_incomplete" for f in after.findings)


def test_inject_writes_numbered_comments(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text(KONNECT_STYLE, encoding="utf-8")
    fields = titleblock.inject_title_block(
        path,
        title="led_loop",
        date="2026-09-24",
        rev="1",
        comments=["LED loop fixture", "Star ground at J1"],
    )
    assert fields["comment_1"] == "LED loop fixture"
    assert fields["comment_2"] == "Star ground at J1"
    root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    block = next(n for n in root if isinstance(n, list) and n and n[0] == "title_block")
    comments = [f for f in block[1:] if isinstance(f, list) and f and f[0] == "comment"]
    assert comments == [["comment", "1", "LED loop fixture"], ["comment", "2", "Star ground at J1"]]


def test_inject_replaces_existing_comment(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text(EMPTY_BLOCK, encoding="utf-8")
    titleblock.inject_title_block(path, title="t", date="d", rev="r", comments=["first"])
    titleblock.inject_title_block(path, title="t", date="d", rev="r", comments=["second"])
    root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    block = next(n for n in root if isinstance(n, list) and n and n[0] == "title_block")
    comments = [f for f in block[1:] if isinstance(f, list) and f and f[0] == "comment"]
    assert comments == [["comment", "1", "second"]]


def test_inject_comment_escapes_specials(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text(KONNECT_STYLE, encoding="utf-8")
    titleblock.inject_title_block(path, title="t", date="d", rev="r", comments=['say "hi"\nline2'])
    root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    block = next(n for n in root if isinstance(n, list) and n and n[0] == "title_block")
    comments = [f for f in block[1:] if isinstance(f, list) and f and f[0] == "comment"]
    assert comments == [["comment", "1", 'say "hi"\nline2']]


def test_comment_clears_notes_absent_lint(tmp_path: Path) -> None:
    symbol = (
        '(symbol (lib_id "Device:R") (at 100 100 0) (unit 1) '
        '(in_bom yes) (on_board yes) (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") '
        '(property "Reference" "R1" (at 101 97 0) (effects (font (size 1.27 1.27)))) '
        '(property "Value" "100" (at 101 103 0) (effects (font (size 1.27 1.27)))))'
    )
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(KONNECT_STYLE.replace("\n)\n", "\n\t" + symbol + "\n)\n"), encoding="utf-8")
    bare = sch_lint.lint_file(sch)
    assert any(f.type == "notes_absent" for f in bare.findings)
    titleblock.inject_title_block(
        sch, title="t", date="d", rev="r", comments=["design intent here"]
    )
    report = sch_lint.lint_file(sch)
    assert not any(f.type == "notes_absent" for f in report.findings)


def test_unbalanced_text_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text('(kicad_sch (paper "A4"', encoding="utf-8")
    with pytest.raises(titleblock.TitleBlockError):
        titleblock.inject_title_block(path, title="t", date="d", rev="r")
