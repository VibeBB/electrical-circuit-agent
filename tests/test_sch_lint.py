from pathlib import Path

from circuit.sch_lint import SchLintError, lint_file, lint_schematic

_HEADER = '(kicad_sch (version 20231120) (generator "eeschema") (paper "A4") (lib_symbols) '
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
