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
_NOTE = '(text "input filter" (at 150 40 0))'


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
    path = _write_sch(tmp_path, hidden + " " + _NOTE)
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


_SYMBOL_TWO = (
    _SYMBOL_OK.replace("(at 100 100 0)", "(at 200 100 0)")
    .replace("(at 101 97 0)", "(at 201 97 0)")
    .replace("(at 101 103 0)", "(at 201 103 0)")
    .replace(
        'uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'uuid "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"',
    )
)
_LABEL = '(label "N1" (at 150 100 0) (effects (font (size 1.27 1.27))))'
_WIRE = (
    "(wire (pts (xy 105 100) (xy 195 100)) "
    '(stroke (width 0) (type default)) (uuid "cccccccc-dddd-eeee-ffff-000000000000"))'
)


def test_lint_warns_when_labels_carry_all_connectivity(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _SYMBOL_TWO + " " + _LABEL)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    finding = next(f for f in report.findings if f.type == "label_only_connectivity")
    assert finding.severity == "warning"


def test_lint_no_label_only_warning_when_wires_present(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _SYMBOL_TWO + " " + _WIRE + " " + _LABEL)
    report = lint_schematic(path)
    assert not any(f.type == "label_only_connectivity" for f in report.findings)


def test_lint_no_label_only_warning_with_single_symbol(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _LABEL)
    report = lint_schematic(path)
    assert not any(f.type == "label_only_connectivity" for f in report.findings)


def test_lint_no_label_only_warning_for_power_symbols(tmp_path: Path) -> None:
    power = _SYMBOL_OK.replace('lib_id "Device:R"', 'lib_id "power:GND"')
    power_two = _SYMBOL_TWO.replace('lib_id "Device:R"', 'lib_id "power:VDD"')
    path = _write_sch(tmp_path, power + " " + power_two + " " + _LABEL)
    report = lint_schematic(path)
    assert not any(f.type == "label_only_connectivity" for f in report.findings)


_WIRE_RUN = (
    "(wire (pts (xy 50 50) (xy 150 50)) "
    '(stroke (width 0) (type default)) (uuid "dddddddd-eeee-ffff-0000-111111111111"))'
)
_WIRE_TAP = (
    "(wire (pts (xy 100 50) (xy 100 90)) "
    '(stroke (width 0) (type default)) (uuid "eeeeeeee-ffff-0000-1111-222222222222"))'
)
_JUNCTION = '(junction (at 100 50) (uuid "ffffffff-0000-1111-2222-333333333333"))'


def test_lint_warns_when_wire_endpoint_taps_mid_run_without_junction(
    tmp_path: Path,
) -> None:
    path = _write_sch(tmp_path, _WIRE_RUN + " " + _WIRE_TAP + " " + _NOTE)
    report = lint_schematic(path)
    assert report.verdict == "pass"
    finding = next(f for f in report.findings if f.type == "junction_missing")
    assert finding.severity == "warning"
    assert "(100.00,50.00)" in finding.description


def test_lint_no_junction_warning_when_dot_present(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _WIRE_RUN + " " + _WIRE_TAP + " " + _JUNCTION + " " + _NOTE)
    report = lint_schematic(path)
    assert not any(f.type == "junction_missing" for f in report.findings)


def test_lint_no_junction_warning_for_endpoint_continuation(tmp_path: Path) -> None:
    onward = (
        "(wire (pts (xy 150 50) (xy 200 50)) "
        "(stroke (width 0) (type default)) "
        '(uuid "00000000-1111-2222-3333-444444444444"))'
    )
    path = _write_sch(tmp_path, _WIRE_RUN + " " + onward + " " + _NOTE)
    report = lint_schematic(path)
    assert not any(f.type == "junction_missing" for f in report.findings)


def test_lint_no_junction_warning_for_wire_crossing(tmp_path: Path) -> None:
    crossing = (
        "(wire (pts (xy 100 30) (xy 100 70)) "
        "(stroke (width 0) (type default)) "
        '(uuid "11111111-2222-3333-4444-555555555555"))'
    )
    path = _write_sch(tmp_path, _WIRE_RUN + " " + crossing + " " + _NOTE)
    report = lint_schematic(path)
    assert not any(f.type == "junction_missing" for f in report.findings)


def test_lint_warns_when_label_floats_off_everything(tmp_path: Path) -> None:
    far_label = _LABEL.replace("(at 150 100 0)", "(at 250 190 0)")
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _WIRE + " " + far_label)
    report = lint_schematic(path)
    finding = next(f for f in report.findings if f.type == "label_off_wire")
    assert finding.severity == "warning"
    assert finding.items and "N1" in finding.items[0]


def test_lint_no_off_wire_warning_for_label_on_wire(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _SYMBOL_TWO + " " + _WIRE + " " + _LABEL)
    report = lint_schematic(path)
    assert not any(f.type == "label_off_wire" for f in report.findings)


def test_lint_no_off_wire_warning_for_label_on_pin_stub(tmp_path: Path) -> None:
    stub_label = _LABEL.replace("(at 150 100 0)", "(at 110 100 0)")
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + stub_label)
    report = lint_schematic(path)
    assert not any(f.type == "label_off_wire" for f in report.findings)


def test_lint_warns_when_sheet_has_no_notes(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK)
    report = lint_schematic(path)
    finding = next(f for f in report.findings if f.type == "notes_absent")
    assert finding.severity == "warning"


def test_lint_no_notes_warning_with_text_note(tmp_path: Path) -> None:
    path = _write_sch(tmp_path, _SYMBOL_OK + " " + _NOTE)
    report = lint_schematic(path)
    assert not any(f.type == "notes_absent" for f in report.findings)


def test_lint_no_notes_warning_with_title_block_comment(tmp_path: Path) -> None:
    text = (
        _HEADER.replace('(rev "1")', '(rev "1") (comment 1 "LED loop fixture")')
        + _SYMBOL_OK
        + _FOOTER
    )
    path = tmp_path / "board.kicad_sch"
    path.write_text(text, encoding="utf-8")
    report = lint_schematic(path)
    assert not any(f.type == "notes_absent" for f in report.findings)
