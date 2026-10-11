from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from circuit import cli
from circuit.brief import DesignBrief
from circuit.handling import export_handling

BRIEF = Path(__file__).parent / "data" / "brief_desk_lamp.json"
FACTS: dict[str, Any] = {
    "msl": "3",
    "peak_reflow_c": 250,
    "esd_sensitive": True,
    "source": "ESP32-S3-WROOM-1 datasheet v1.4, section 8",
}


def _brief(tmp_path: Path, handled: dict[str, dict[str, Any]]) -> Path:
    value: dict[str, Any] = json.loads(BRIEF.read_text(encoding="utf-8"))
    for part in value["parts"]:
        if part["reference"] in handled:
            part["handling"] = handled[part["reference"]]
    path = tmp_path / "desk_lamp.brief.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _all(tmp_path: Path) -> Path:
    refs = [p["reference"] for p in json.loads(BRIEF.read_text(encoding="utf-8"))["parts"]]
    return _brief(tmp_path, {ref: FACTS for ref in refs})


def _export(path: Path) -> Any:
    return export_handling(DesignBrief.model_validate_json(path.read_text(encoding="utf-8")), path)


def test_every_handled_part_is_exported_verbatim(tmp_path: Path) -> None:
    result = _export(_all(tmp_path))
    assert (result.verdict, result.unknown) == ("pass", [])
    assert [p.ref for p in result.parts] == sorted(p.ref for p in result.parts)
    u1 = next(p for p in result.parts if p.ref == "U1")
    assert (u1.msl, u1.peak_reflow_c, u1.esd_sensitive) == ("3", 250, True)


def test_part_without_handling_is_unknown(tmp_path: Path) -> None:
    result = _export(_brief(tmp_path, {"U1": FACTS}))
    assert result.verdict == "unknown"
    assert "R1" in result.unknown and "U1" not in result.unknown
    assert [p.ref for p in result.parts] == ["U1"]


@pytest.mark.parametrize(
    "bad",
    [{"msl": "7"}, {"peak_reflow_c": 0}, {"source": ""}, {"extra": 1}, {"esd_sensitive": None}],
)
def test_invalid_handling_is_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    path = _brief(tmp_path, {"U1": {**FACTS, **bad}})
    with pytest.raises(ValueError):
        DesignBrief.model_validate_json(path.read_text(encoding="utf-8"))


def test_cli_exit_follows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "desk_lamp.circuit-handling.json"
    assert cli.main(["handling", "--brief", str(_all(tmp_path)), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["artifact_kind"] == "circuit_handling"
    capsys.readouterr()
    assert cli.main(["handling", "--brief", str(BRIEF)]) == 1
