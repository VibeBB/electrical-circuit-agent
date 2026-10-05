"""ISO 7200 drawing sheet and project text variables."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit import drawing_sheet, sexpr
from circuit.brief import DesignBrief, DrawingInfo

BASE = {
    "name": "led_loop",
    "parts": [
        {
            "reference": "R1",
            "lib_id": "Device:R",
            "footprint": "Resistor_SMD:R_0603_1608Metric",
            "value": "1k",
        }
    ],
    "nets": [{"name": "N1", "pins": ["R1.1", "R1.2"]}],
    "board": {"width_mm": 20, "height_mm": 20},
}


def _brief(**drawing: str) -> DesignBrief:
    return DesignBrief.model_validate({**BASE, "drawing": drawing})


def test_status_is_derived_from_release_fields() -> None:
    assert DrawingInfo().status == "In preparation"
    assert DrawingInfo(approved_by="K. T").status == "In approval"
    assert DrawingInfo(approved_by="K. T", date_of_issue="2026-10-05").status == "Released"


def test_issue_date_requires_an_approver() -> None:
    with pytest.raises(ValidationError, match="requires approved_by"):
        DrawingInfo(date_of_issue="2026-10-05")


def test_issue_date_must_be_a_calendar_date() -> None:
    with pytest.raises(ValidationError, match="calendar date"):
        DrawingInfo(approved_by="K. T", date_of_issue="2026-13-01")


def test_variables_default_to_dashes_and_name() -> None:
    values = drawing_sheet.variables(_brief(), brief_sha256="ab" * 32)
    assert values["VIBEBB_ID"] == "led_loop"
    assert values["VIBEBB_STATUS"] == "In preparation"
    assert values["VIBEBB_OWNER"] == drawing_sheet.BLANK
    assert values["VIBEBB_DATE"] == drawing_sheet.BLANK
    assert values["VIBEBB_BRIEF_SHA256"] == "ab" * 8
    assert values["VIBEBB_GENERATOR"].startswith("circuit-agent/")


def test_variables_carry_drawing_metadata() -> None:
    values = drawing_sheet.variables(
        _brief(
            legal_owner="ACME K.K.",
            identification_prefix="ACME-PCB-001",
            approved_by="K. T",
            date_of_issue="2026-10-05",
            language="ja",
        ),
        brief_sha256="cd" * 32,
    )
    assert values["VIBEBB_OWNER"] == "ACME K.K."
    assert values["VIBEBB_ID"] == "ACME-PCB-001"
    assert values["VIBEBB_STATUS"] == "Released"
    assert values["VIBEBB_DATE"] == "2026-10-05"
    assert values["VIBEBB_LANGUAGE"] == "ja"


def test_sheet_text_is_deterministic_and_parses() -> None:
    text = drawing_sheet.sheet_text()
    assert text == drawing_sheet.sheet_text()
    root = sexpr.parse_text(text)
    assert root[0] == "kicad_wks"
    for variable, *_ in drawing_sheet.CELLS:
        assert ("${" + variable + "}") in text or variable == "#"
    assert "${#}/${##}" in text
    assert "VibeBB" not in text


def test_cells_tile_the_title_block() -> None:
    for _, _, left, right, top, bottom, _ in drawing_sheet.CELLS:
        assert 0 <= left < right <= drawing_sheet.TITLE_BLOCK_WIDTH_MM
        assert 0 <= top < bottom <= 6
    bottom_right = [c for c in drawing_sheet.CELLS if c[3] == 180.0 and c[5] == 6]
    assert [c[0] for c in bottom_right] == ["#"]


def test_apply_preserves_project_settings(tmp_path: Path) -> None:
    project = tmp_path / "led_loop.kicad_pro"
    project.write_text(
        json.dumps(
            {
                "meta": {"filename": "led_loop.kicad_pro", "version": 3},
                "schematic": {"drawing": {"default_line_thickness": 6.0}},
                "text_variables": {"KEEP": "1", "VIBEBB_STALE": "x"},
            }
        ),
        encoding="utf-8",
    )
    sheet = drawing_sheet.apply(project, text_variables={"VIBEBB_ID": "X"})
    data = json.loads(project.read_text(encoding="utf-8"))
    assert sheet == tmp_path / "led_loop.kicad_wks"
    assert sheet.read_text(encoding="utf-8") == drawing_sheet.sheet_text()
    assert data["schematic"]["page_layout_descr_file"] == "led_loop.kicad_wks"
    assert data["schematic"]["drawing"] == {"default_line_thickness": 6.0}
    assert data["text_variables"] == {"KEEP": "1", "VIBEBB_ID": "X"}
    assert data["meta"]["version"] == 3


@pytest.mark.parametrize("body", ["", "not json", "[]", '{"schematic": 1}'])
def test_apply_fails_closed_on_bad_projects(tmp_path: Path, body: str) -> None:
    project = tmp_path / "p.kicad_pro"
    project.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        drawing_sheet.apply(project, text_variables={})


def test_apply_fails_closed_on_missing_project(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        drawing_sheet.apply(tmp_path / "missing.kicad_pro", text_variables={})
