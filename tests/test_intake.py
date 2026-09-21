import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit.brief import load_brief
from circuit.intake import Intake, check_intake, load_intake

ROOT = Path(__file__).parent


def test_fixture_intake_is_ready() -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    intake_path = ROOT / "data" / "brief_led_loop.intake.json"
    result = check_intake(
        load_brief(brief_path),
        load_intake(intake_path),
        brief_path=brief_path,
        intake_path=intake_path,
    )
    assert result.verdict == "ready"
    assert result.assumption_only_parts == []
    assert result.assumption_only_nets == []


def _fixture_value() -> dict[str, Any]:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    value = json.loads((ROOT / "data" / "brief_led_loop.intake.json").read_text(encoding="utf-8"))
    value["brief_sha256"] = hashlib.sha256(brief_path.read_bytes()).hexdigest()
    return value


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"brief_sha256": "0" * 64}, "brief sha256 mismatch"),
        ({"part_sources": {"R1": ["R1"], "D1": ["R1"]}}, "unmapped parts: J1"),
        (
            {"net_sources": {"VIN": ["R2"], "LED_A": ["R1"], "GND": ["R2"], "EXTRA": ["R1"]}},
            "unknown nets: EXTRA",
        ),
        ({"part_sources": {"J1": ["R9"], "R1": ["R1", "R3"], "D1": ["R1"]}}, "unknown source ids"),
    ],
)
def test_intake_blocked(tmp_path: Path, change: dict[str, object], reason: str) -> None:
    value = _fixture_value()
    value.update(change)
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    brief_path = ROOT / "data" / "brief_led_loop.json"
    result = check_intake(
        load_brief(brief_path),
        load_intake(intake_path),
        brief_path=brief_path,
        intake_path=intake_path,
    )
    assert result.verdict == "blocked"
    assert any(reason in item for item in result.reasons)


def test_open_questions_block_and_list_ids(tmp_path: Path) -> None:
    value = _fixture_value()
    value["open_questions"] = [
        {"id": "Q1", "text": "電源電圧は?"},
        {"id": "Q2", "text": "確認?"},
    ]
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    brief_path = ROOT / "data" / "brief_led_loop.json"
    result = check_intake(
        load_brief(brief_path),
        load_intake(intake_path),
        brief_path=brief_path,
        intake_path=intake_path,
    )
    assert result.verdict == "blocked"
    assert "open questions: Q1, Q2" in result.reasons


def test_assumption_only_net_is_informational(tmp_path: Path) -> None:
    value = _fixture_value()
    value["net_sources"]["VIN"] = ["A1"]
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    brief_path = ROOT / "data" / "brief_led_loop.json"
    result = check_intake(
        load_brief(brief_path),
        load_intake(intake_path),
        brief_path=brief_path,
        intake_path=intake_path,
    )
    assert result.verdict == "ready"
    assert result.assumption_only_nets == ["VIN"]


def test_duplicate_ids_rejected() -> None:
    value = _fixture_value()
    value["assumptions"] = [{"id": "R1", "text": "bad", "rationale": "bad"}]
    with pytest.raises(ValidationError):
        Intake.model_validate(value)
