import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit import cli
from circuit.brief import DesignBrief
from circuit.derating import check_derating

FIXTURE = Path(__file__).parent / "data" / "brief_led_loop.json"


def _brief(tmp_path: Path, rules: list[dict[str, Any]], **part_changes: Any) -> Path:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["derating"] = {"rules": rules}
    for part in value["parts"]:
        part.update(part_changes.get(part["reference"], {}))
    path = tmp_path / "led_loop.brief.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _report(path: Path) -> Any:
    return check_derating(DesignBrief.model_validate_json(path.read_text(encoding="utf-8")), path)


RULE = {"references": ["J1"], "voltage_factor": 0.8, "current_factor": 0.5, "source": "IPC-9592B"}


def test_within_derated_ratings_passes(tmp_path: Path) -> None:
    report = _report(_brief(tmp_path, [RULE]))
    assert report.verdict == "pass"
    current, voltage = report.checks
    assert (current.quantity, current.applied, current.limit) == ("current", 0.06, 1.5)
    assert (voltage.quantity, voltage.applied, voltage.limit) == ("voltage", 12.0, 200.0)


@pytest.mark.parametrize(("rated", "status"), [(15.0, "pass"), (14.99, "fail")])
def test_voltage_boundary(tmp_path: Path, rated: float, status: str) -> None:
    rule = {"references": ["J1"], "voltage_factor": 0.8, "source": "IPC-9592B"}
    report = _report(_brief(tmp_path, [rule], J1={"rated_voltage_v": rated}))
    assert report.verdict == status


def test_missing_rating_is_unknown(tmp_path: Path) -> None:
    rule = {"references": ["R1"], "voltage_factor": 0.5, "source": "IPC-9592B"}
    report = _report(_brief(tmp_path, [rule]))
    assert report.verdict == "unknown"
    assert report.checks[0].detail == "R1 has no rated_voltage_v"


def test_unstated_net_stress_is_unknown(tmp_path: Path) -> None:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for net in value["nets"]:
        net["current_a"] = 0.0
    value["derating"] = {"rules": [{"references": ["J1"], "current_factor": 0.5, "source": "x"}]}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert _report(path).verdict == "unknown"


@pytest.mark.parametrize(
    "rules",
    [
        [{"references": ["J1"], "source": "x"}],
        [{"references": ["J1"], "voltage_factor": 1.2, "source": "x"}],
        [{"references": ["J1"], "voltage_factor": 0.8, "source": ""}],
        [{"references": ["Q9"], "voltage_factor": 0.8, "source": "x"}],
        [RULE, RULE],
    ],
)
def test_invalid_rules_are_rejected(tmp_path: Path, rules: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        _report(_brief(tmp_path, rules))


def test_cli_exit_follows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "led_loop.derating.json"
    path = _brief(tmp_path, [RULE], J1={"rated_current_a": 0.1})
    assert cli.main(["derating", "--brief", str(path), "--out", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "fail"
    capsys.readouterr()
    no_section = tmp_path / "plain.json"
    no_section.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.main(["derating", "--brief", str(no_section)]) == 1
