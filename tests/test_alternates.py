import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit import cli
from circuit.alternates import check_alternates
from circuit.brief import DesignBrief

FIXTURE = Path(__file__).parent / "data" / "brief_led_loop.json"
J1_ALT: dict[str, Any] = {
    "mpn": "XH-2P-ALT",
    "manufacturer": "Second Source Co",
    "lib_id": "Connector_Generic:Conn_01x02",
    "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "rated_current_a": 3.0,
    "rated_voltage_v": 250.0,
    "source": "XH-2P-ALT datasheet rev B, table 1",
}


def _brief(tmp_path: Path, alternates: list[dict[str, Any]], reference: str = "J1") -> Path:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for part in value["parts"]:
        if part["reference"] == reference:
            part["alternates"] = alternates
    path = tmp_path / "led_loop.brief.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _report(path: Path) -> Any:
    return check_alternates(DesignBrief.model_validate_json(path.read_text(encoding="utf-8")), path)


def test_drop_in_alternate_passes(tmp_path: Path) -> None:
    report = _report(_brief(tmp_path, [J1_ALT]))
    assert report.verdict == "pass"
    assert [c.aspect for c in report.checks] == ["current", "footprint", "symbol", "voltage"]
    (entry,) = report.alternates
    assert (entry.reference, entry.mpn, entry.status) == ("J1", "XH-2P-ALT", "pass")
    assert len(report.brief_sha256) == 64


@pytest.mark.parametrize(
    ("changes", "aspect", "status"),
    [
        ({"rated_voltage_v": 249.99}, "voltage", "fail"),
        ({"rated_current_a": 2.99}, "current", "fail"),
        ({"rated_current_a": None}, "current", "unknown"),
        ({"footprint": "Connector_JST:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical"}, "footprint", "fail"),
        ({"lib_id": "Connector:Conn_01x02_Pin"}, "symbol", "unknown"),
    ],
)
def test_non_drop_in_alternate_does_not_pass(
    tmp_path: Path, changes: dict[str, Any], aspect: str, status: str
) -> None:
    report = _report(_brief(tmp_path, [J1_ALT | changes]))
    assert report.verdict == status
    assert {c.aspect: c.status for c in report.checks}[aspect] == status
    assert report.alternates[0].status == status


def test_unrated_primary_skips_rating_checks(tmp_path: Path) -> None:
    alt = {
        "mpn": "MFR-25FBF52-330R",
        "manufacturer": "Yageo",
        "lib_id": "Device:R",
        "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
        "source": "MFR series datasheet",
    }
    report = _report(_brief(tmp_path, [alt], reference="R1"))
    assert report.verdict == "pass"
    assert [c.aspect for c in report.checks] == ["footprint", "symbol"]


@pytest.mark.parametrize(
    "alternates",
    [
        [J1_ALT | {"source": ""}],
        [J1_ALT | {"footprint": "NoColon"}],
        [J1_ALT | {"rated_voltage_v": 0}],
        [J1_ALT | {"extra": 1}],
        [J1_ALT, J1_ALT | {"mpn": "xh-2p-alt"}],
    ],
)
def test_invalid_alternates_are_rejected(tmp_path: Path, alternates: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        _report(_brief(tmp_path, alternates))


def test_cli_exit_follows_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "led_loop.alternates.json"
    path = _brief(tmp_path, [J1_ALT | {"rated_current_a": 1.0}])
    assert cli.main(["alternates", "--brief", str(path), "--out", str(out)]) == 1
    written = json.loads(out.read_text(encoding="utf-8"))
    assert (written["artifact_kind"], written["verdict"]) == ("circuit_alternates", "fail")
    assert cli.main(["alternates", "--brief", str(_brief(tmp_path, [J1_ALT]))]) == 0
    capsys.readouterr()
    plain = tmp_path / "plain.json"
    plain.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.main(["alternates", "--brief", str(plain)]) == 1
