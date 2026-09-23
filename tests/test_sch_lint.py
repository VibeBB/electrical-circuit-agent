from pathlib import Path

from circuit.sch_lint import SchLintError, lint_file, lint_schematic

_HEADER = (
    '(kicad_sch (version 20231120) (generator "eeschema") (paper "A4") (lib_symbols) '
    '(title_block (title "fixture") (date "2026-09-23") (rev "1")) '
)
_FOOTER = ")"

_SYMBOL_OK = (
    '(symbol (lib_id "Device:R") (at 100 100 0) (unit 1) '
    '(in_bom yes) (on_board yes) (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") '
    '(property "Reference" "R1" (at 101 97 0) '
    "(effects (font (size 1.27 1.27)))) "
    '(property "Value" "100" (at 101 103 0) '
    "(effects (font (size 1.27 1.27)))))"
)


def _write_sch(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "board.kicad_sch"
    path.write_text(_HEADER + body + _FOOTER, encoding="utf-8")
    return path


def test_lint_passes_on_well_formed_schematic(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    assert report.symbols_checked == 1
    assert report.errors == 0


def test_lint_fails_when_property_is_far_from_symbol(tmp_path: Path) -> None:
    broken = _SYMBOL_OK.replace("(at 101 97 0)", "(at 0 3.81 0)")
    path = _write_sch(tmp_path, broken)
    report = lint_schematic(path)
    assert report.verdict == "fail"
    assert any(f.type == "property_far_from_symbol" for f in report.findings)


def test_lint_fails_when_symbol_is_out_of_bounds(tmp_path: Path) -> None:
    broken = (
        _SYMBOL_OK.replace("(at 100 100 0)", "(at -10 100 0)")
        .replace("(at 101 97 0)", "(at -9 97 0)")
        .replace("(at 101 103 0)", "(at -9 103 0)")
    )
    path = _write_sch(tmp_path, broken)
    report = lint_schematic(path)
    assert report.verdict == "fail"
    assert any(f.type == "item_out_of_bounds" for f in report.findings)


def test_lint_warns_on_hidden_property_without_failing(tmp_path: Path) -> None:
    hidden = _SYMBOL_OK.replace(
        "(effects (font (size 1.27 1.27))))",
        "(effects (font (size 1.27 1.27)) (justify left) hide))",
        1,
    )
    path = _write_sch(tmp_path, hidden)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    assert report.warnings == 1
    assert report.findings[0].type == "property_hidden"


def test_lint_is_fail_closed_on_unparseable_input(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text("not-a-schematic (((", encoding="utf-8")
    report = lint_file(path)
    assert report.verdict == "fail"
    assert report.errors == 1
    assert report.findings[0].type == "parse_error"


def test_lint_rejects_non_kicad_sch_root(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_text("(kicad_pcb (version 4))", encoding="utf-8")
    try:
        lint_schematic(path)
    except SchLintError as exc:
        assert "not a kicad_sch" in str(exc)
    else:
        raise AssertionError("expected SchLintError")


def test_lint_warns_when_property_sits_on_symbol(tmp_path: Path) -> None:
    on_body = _SYMBOL_OK.replace("(at 101 103 0)", "(at 100 100 0)")
    path = _write_sch(tmp_path, on_body)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    assert any(f.type == "property_on_symbol" for f in report.findings)
    finding = next(f for f in report.findings if f.type == "property_on_symbol")
    assert finding.severity == "warning"


def test_lint_warns_on_empty_title_block(tmp_path: Path) -> None:
    body = _SYMBOL_OK
    text = (
        '(kicad_sch (version 20231120) (generator "eeschema") (paper "A4") '
        '(lib_symbols) (title_block (title "") (date "") (rev "")) ' + body + ")"
    )
    path = tmp_path / "board.kicad_sch"
    path.write_text(text, encoding="utf-8")
    report = lint_schematic(path)
    assert report.verdict == "pass"
    assert any(f.type == "title_block_incomplete" for f in report.findings)


def test_lint_warns_when_title_block_is_missing(tmp_path: Path) -> None:
    text = (
        '(kicad_sch (version 20231120) (generator "eeschema") (paper "A4") '
        "(lib_symbols) " + _SYMBOL_OK + ")"
    )
    path = tmp_path / "board.kicad_sch"
    path.write_text(text, encoding="utf-8")
    report = lint_schematic(path)
    assert any(f.type == "title_block_incomplete" for f in report.findings)


def test_lint_warns_on_clustered_placement(tmp_path: Path) -> None:
    second = _SYMBOL_OK.replace("(at 100 100 0)", "(at 110 110 0)").replace(
        'uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'uuid "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"',
    )
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + second)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    assert any(f.type == "sheet_underutilized" for f in report.findings)


def test_lint_no_underutilized_warning_for_spread_placement(tmp_path: Path) -> None:
    second = (
        _SYMBOL_OK.replace("(at 100 100 0)", "(at 285 205 0)")
        .replace("(at 101 97 0)", "(at 286 202 0)")
        .replace("(at 101 103 0)", "(at 286 208 0)")
        .replace(
            'uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
            'uuid "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"',
        )
    )
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + second)
    report = lint_schematic(path)
    assert not any(f.type == "sheet_underutilized" for f in report.findings)
