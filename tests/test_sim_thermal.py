from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from circuit import cli
from circuit.brief import DesignBrief
from circuit.sim_thermal import (
    expected_request,
    lifetime_brief,
    resolve_response,
    thermal_brief,
    thermal_check,
    thermal_findings,
    write_sim_request,
)

DATA = Path(__file__).parent / "data" / "brief_led_loop.json"
THERMAL: dict[str, Any] = {
    "ambient_c": 40,
    "parts": [
        {
            "reference": "R1",
            "power_w": 0.25,
            "tj_max_c": 155,
            "derating_margin_c": 10,
            "theta_ja_c_per_w": 200,
            "source": "Yageo CFR datasheet rev 2024, derating curve",
        },
        {
            "reference": "D1",
            "power_w": 0.12,
            "tj_max_c": 100,
            "theta_jc": 150,
            "theta_sa": 250,
            "source": "LED datasheet table 3",
        },
    ],
    "response_path": "sim/led_loop.thermal.sim-response.json",
}
PASSING: list[dict[str, Any]] = [
    {"id": "thermal.R1.tj", "verdict": "pass", "measured": 90.0, "limit": "≤ 145 °C"},
    {"id": "thermal.D1.tj", "verdict": "pass", "measured": 88.0, "limit": "≤ 100 °C"},
]


def _brief(**changes: Any) -> DesignBrief:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    return DesignBrief.model_validate({**data, "thermal": {**THERMAL, **changes}})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _answer(
    root: Path,
    design: DesignBrief,
    checks: list[dict[str, Any]],
    *,
    status: str | None = None,
    verdict: str | None = None,
    report_verdict: str | None = None,
    **overrides: Any,
) -> Path:
    """Play simulation-agent: write request (circuit), report and response (sim)."""
    out = write_sim_request(design, root / "sim", root=root)
    request_path = Path(out["request"])
    rows: list[dict[str, Any]] = [
        {"analysis": "thermal", "detail": "", "evidence": [], **c} for c in checks
    ]
    statuses = {c["verdict"] for c in rows}
    aggregate = "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    verdict = verdict or aggregate
    report = root / "out" / f"{design.name}-thermal" / "sim-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"schema_version": 1, "verdict": report_verdict or verdict, "checks": rows}),
        encoding="utf-8",
    )
    response: dict[str, Any] = {
        "schema_version": 2,
        "request_id": out["request_id"],
        "request_sha256": _sha(request_path),
        "brief_sha256": out["sim_brief_sha256"],
        "status": status
        or {"pass": "accepted", "fail": "rejected", "unknown": "needs_info"}[verdict],
        "verdict": verdict,
        "report_path": str(report),
        "sha256": _sha(report),
        "decision_refs": [],
        "reasons": [],
        **overrides,
    }
    path = root / "sim" / "led_loop.thermal.sim-response.json"
    path.write_text(json.dumps(response), encoding="utf-8")
    return path


def _status(design: DesignBrief, path: Path | None, root: Path) -> dict[str, str]:
    return {subject: status for subject, status, _, _ in thermal_findings(design, path, root)}


def test_thermal_brief_mirrors_simulation_schema() -> None:
    payload = thermal_brief(_brief())
    assert payload["schema_version"] == 1
    assert payload["thermal"]["ambient_c"] == 40
    r1, d1 = payload["thermal"]["components"]
    assert r1 == {
        "ref": "R1",
        "power_w": 0.25,
        "tj_max_c": 155,
        "derating_margin_c": 10,
        "path": {"theta_ja_c_per_w": 200},
    }
    assert d1["path"] == {"theta_jc": 150, "theta_sa": 250}
    assert "Yageo CFR datasheet" in payload["description"]


def test_part_without_thermal_path_is_left_for_simulation() -> None:
    bare = {"reference": "R1", "power_w": 0.1, "tj_max_c": 125, "source": "datasheet"}
    payload = thermal_brief(_brief(parts=[bare]))
    assert "path" not in payload["thermal"]["components"][0]


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ([{**THERMAL["parts"][0], "reference": "U9"}], "unknown part"),
        ([THERMAL["parts"][0], THERMAL["parts"][0]], "unique"),
        ([{**THERMAL["parts"][0], "source": ""}], "source"),
        ([{**THERMAL["parts"][0], "power_w": -1}], "power_w"),
        ([], "parts"),
    ],
)
def test_thermal_spec_rejects_bad_parts(parts: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _brief(parts=parts)


def test_request_is_hash_bound_and_ignores_response_path(tmp_path: Path) -> None:
    design = _brief()
    out = write_sim_request(design, tmp_path / "sim", root=tmp_path)
    request = json.loads(Path(out["request"]).read_text(encoding="utf-8"))
    assert request["from_system"] == "circuit"
    assert request["kind"] == "thermal"
    assert request["brief_path"] == "sim/led_loop.thermal.sim.json"
    assert out["sim_brief_sha256"] == _sha(tmp_path / request["brief_path"])
    assert request["request_id"] == f"led_loop-thermal-{out['sim_brief_sha256'][:12]}"
    assert expected_request(_brief(response_path="x.sim-response.json")) == expected_request(design)


def test_accepted_response_reports_sim_checks(tmp_path: Path) -> None:
    design = _brief()
    path = _answer(tmp_path, design, PASSING)
    result = thermal_check(design, path, tmp_path)
    assert result["verdict"] == "pass"
    assert [(c["subject"], c["status"]) for c in result["checks"]] == [
        ("response", "pass"),
        ("R1.tj", "pass"),
        ("D1.tj", "pass"),
    ]
    assert result["checks"][1]["measured"] == 90.0
    assert "limit ≤ 145 °C" in result["checks"][1]["detail"]


def test_simulation_fail_is_never_promoted(tmp_path: Path) -> None:
    design = _brief()
    failing = [PASSING[0], {**PASSING[1], "verdict": "fail", "measured": 120.0}]
    path = _answer(tmp_path, design, failing)
    result = thermal_check(design, path, tmp_path)
    assert result["verdict"] == "fail"
    assert _status(design, path, tmp_path)["D1.tj"] == "fail"


def test_failing_check_carries_margin_and_guidance(tmp_path: Path) -> None:
    design = _brief()
    failing = [
        PASSING[0],
        {
            **PASSING[1],
            "verdict": "fail",
            "measured": 160.0,
            "margin": -15.0,
            "guidance": ["power_w(D1) ≤ 0.5 W at the current θ", None],
        },
    ]
    path = _answer(tmp_path, design, failing)
    detail = thermal_check(design, path, tmp_path)["checks"][2]["detail"]
    assert "margin -15" in detail
    assert detail.endswith("fix: power_w(D1) ≤ 0.5 W at the current θ")


@pytest.mark.parametrize("status", ["needs_info", "deferred"])
def test_unanswered_response_is_unknown(tmp_path: Path, status: str) -> None:
    design = _brief()
    path = _answer(tmp_path, design, PASSING, status=status, verdict="unknown", reasons=["theta"])
    result = thermal_check(design, path, tmp_path)
    assert result["verdict"] == "unknown"
    assert "theta" in result["checks"][0]["detail"]


def test_missing_section_or_response_is_unknown(tmp_path: Path) -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    plain = DesignBrief.model_validate(data)
    assert thermal_findings(plain, None, tmp_path) == []
    assert thermal_check(plain, None, tmp_path)["verdict"] == "unknown"
    assert resolve_response(plain, tmp_path) is None
    unset = _brief(response_path=None)
    assert _status(unset, None, tmp_path) == {"response": "unknown"}
    design = _brief()
    path = resolve_response(design, tmp_path)
    assert path == tmp_path / "sim" / "led_loop.thermal.sim-response.json"
    assert _status(design, path, tmp_path) == {"response": "unknown"}
    assert _status(design, tmp_path / "x.json", tmp_path) == {"response": "unknown"}


def test_stale_brief_fails(tmp_path: Path) -> None:
    path = _answer(tmp_path, _brief(), PASSING)
    hotter = _brief(ambient_c=60)
    findings = thermal_findings(hotter, path, tmp_path)
    assert [(s, v) for s, v, _, _ in findings] == [("response", "fail")]
    assert expected_request(hotter)[0] in findings[0][3]


def test_tampered_or_missing_files_fail_closed(tmp_path: Path) -> None:
    design = _brief()
    path = _answer(tmp_path, design, PASSING)
    request = tmp_path / "sim" / "led_loop.thermal.sim-request.json"
    request.write_text(request.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert _status(design, path, tmp_path) == {"response": "fail"}
    path = _answer(tmp_path, design, PASSING)
    report = tmp_path / "out" / "led_loop-thermal" / "sim-report.json"
    report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert _status(design, path, tmp_path) == {"report": "fail"}
    report.unlink()
    assert _status(design, path, tmp_path) == {"report": "unknown"}
    request.unlink()
    assert _status(design, path, tmp_path) == {"response": "unknown"}


def test_report_outside_workspace_is_unknown(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    design = _brief()
    path = _answer(root, design, PASSING)
    outside = tmp_path / "sim-report.json"
    outside.write_text("{}", encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "report_path": str(outside)}), encoding="utf-8")
    assert _status(design, path, root) == {"report": "unknown"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "accepted", "verdict": "fail"},
        {"status": "rejected", "verdict": "pass"},
        {"sha256": None},
        {"schema_version": 1},
        {"extra": True},
        {"request_sha256": "not-a-hash"},
    ],
)
def test_malformed_response_is_unknown(tmp_path: Path, overrides: dict[str, Any]) -> None:
    design = _brief()
    path = _answer(tmp_path, design, PASSING)
    data = {**json.loads(path.read_text(encoding="utf-8")), **overrides}
    path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}), encoding="utf-8")
    assert _status(design, path, tmp_path) == {"response": "unknown"}


def test_other_requester_fails(tmp_path: Path) -> None:
    design = _brief()
    path = _answer(tmp_path, design, PASSING)
    request = tmp_path / "sim" / "led_loop.thermal.sim-request.json"
    data = json.loads(request.read_text(encoding="utf-8"))
    request.write_text(json.dumps({**data, "from_system": "mech"}), encoding="utf-8")
    response = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**response, "request_sha256": _sha(request)}), encoding="utf-8")
    assert _status(design, path, tmp_path) == {"response": "fail"}


@pytest.mark.parametrize(
    ("checks", "report_verdict", "expected"),
    [
        ([], None, {"report": "unknown"}),
        ([{"id": "ruggedness.drop.peak_g", "verdict": "pass"}], None, {"report": "unknown"}),
        ([{"id": "thermal.R1.tj", "verdict": "maybe"}], "pass", {"report": "unknown"}),
        (PASSING, "fail", {"report": "fail"}),
    ],
)
def test_unusable_report_fails_closed(
    tmp_path: Path,
    checks: list[dict[str, Any]],
    report_verdict: str | None,
    expected: dict[str, str],
) -> None:
    design = _brief()
    path = _answer(tmp_path, design, checks, verdict="pass", report_verdict=report_verdict)
    assert _status(design, path, tmp_path) == expected


def test_cli_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    data = json.loads(DATA.read_text(encoding="utf-8"))
    brief_path = tmp_path / "led_loop.brief.json"
    brief_path.write_text(json.dumps({**data, "thermal": THERMAL}), encoding="utf-8")
    assert (
        cli.main(["sim-request", "--brief", str(brief_path), "--out-dir", str(tmp_path / "sim")])
        == 0
    )
    request = json.loads(capsys.readouterr().out)
    assert request["sim_brief"] == "sim/led_loop.thermal.sim.json"
    assert cli.main(["sim-check", "--brief", str(brief_path)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "unknown"
    _answer(tmp_path, _brief(), PASSING)
    assert cli.main(["sim-check", "--brief", str(brief_path)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    plain = tmp_path / "plain.brief.json"
    plain.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["sim-request", "--brief", str(plain)]) == 1
    assert "no thermal section" in capsys.readouterr().out


LIFETIME: dict[str, Any] = {
    "parts": [
        {
            "reference": "R1",
            "rated_life_h": 2000,
            "rated_temp_c": 105,
            "activation_energy_ev": 0.94,
            "profile": [
                {"temperature_c": 70, "fraction": 0.25},
                {"temperature_c": 45, "fraction": 0.75},
            ],
            "required_life_h": 40000,
            "source": "Nichicon UHE datasheet p.2; Ea from vendor life note",
        }
    ],
    "response_path": "sim/led_loop.lifetime.sim-response.json",
}


def _life_brief(**changes: Any) -> DesignBrief:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    return DesignBrief.model_validate({**data, "lifetime": {**LIFETIME, **changes}})


def _life_answer(root: Path, design: DesignBrief, verdict: str) -> Path:
    out = write_sim_request(design, root / "sim", root=root, kind="lifetime")
    request_path = Path(out["request"])
    rows: list[dict[str, Any]] = [
        {
            "id": "lifetime.R1.life_h",
            "analysis": "lifetime",
            "verdict": verdict,
            "detail": "Arrhenius life",
            "measured": 52000.0 if verdict == "pass" else 21000.0,
            "limit": "≥ 40000 h",
            "evidence": [],
        },
        {"id": "thermal.R1.tj", "analysis": "thermal", "verdict": "pass", "evidence": []},
    ]
    report = root / "out" / "lifetime" / "sim-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"schema_version": 1, "verdict": verdict, "checks": rows}), encoding="utf-8"
    )
    response = {
        "schema_version": 2,
        "request_id": out["request_id"],
        "request_sha256": _sha(request_path),
        "brief_sha256": out["sim_brief_sha256"],
        "status": "accepted" if verdict == "pass" else "rejected",
        "verdict": verdict,
        "report_path": str(report),
        "sha256": _sha(report),
    }
    path = root / "sim" / "led_loop.lifetime.sim-response.json"
    path.write_text(json.dumps(response), encoding="utf-8")
    return path


def test_lifetime_brief_mirrors_simulation_schema() -> None:
    payload = lifetime_brief(_life_brief())
    assert payload["lifetime"]["model"] == "arrhenius"
    (part,) = payload["lifetime"]["parts"]
    assert part["ref"] == "R1"
    assert part["activation_energy_ev"] == 0.94
    assert part["profile"][1] == {"temperature_c": 45, "fraction": 0.75}
    assert "Nichicon" in part["source"]


def test_lifetime_request_is_its_own_kind(tmp_path: Path) -> None:
    design = _life_brief()
    out = write_sim_request(design, tmp_path / "sim", root=tmp_path, kind="lifetime")
    request = json.loads(Path(out["request"]).read_text(encoding="utf-8"))
    assert request["kind"] == "lifetime"
    assert request["brief_path"] == "sim/led_loop.lifetime.sim.json"
    assert out["request_id"] == expected_request(design, "lifetime")[0]
    assert out["request_id"].startswith("led_loop-lifetime-")


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_lifetime_round_trip_keeps_simulation_verdict(tmp_path: Path, verdict: str) -> None:
    design = _life_brief()
    path = _life_answer(tmp_path, design, verdict)
    result = thermal_check(design, path, tmp_path, "lifetime")
    assert result["verdict"] == verdict
    subjects = [check["subject"] for check in result["checks"]]
    assert subjects == ["response", "R1.life_h"]


def test_lifetime_stale_brief_and_missing_section(tmp_path: Path) -> None:
    path = _life_answer(tmp_path, _life_brief(), "pass")
    changed = _life_brief(
        parts=[{**LIFETIME["parts"][0], "required_life_h": 60000}],
    )
    assert thermal_check(changed, path, tmp_path, "lifetime")["verdict"] == "fail"
    assert thermal_check(_brief(), path, tmp_path, "lifetime")["verdict"] == "unknown"
    assert thermal_check(_life_brief(), path, tmp_path, "thermal")["verdict"] == "unknown"


def test_thermal_response_does_not_answer_a_lifetime_check(tmp_path: Path) -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    design = DesignBrief.model_validate({**data, "thermal": THERMAL, "lifetime": LIFETIME})
    thermal_path = _answer(tmp_path, design, PASSING)
    assert thermal_check(design, thermal_path, tmp_path, "lifetime")["verdict"] == "fail"


@pytest.mark.parametrize(
    ("part", "message"),
    [
        ({"profile": [{"temperature_c": 60, "fraction": 0.5}]}, "sum to 1"),
        ({"activation_energy_ev": 0}, "activation_energy_ev"),
        ({"source": ""}, "source"),
        ({"reference": "U9"}, "unknown part"),
    ],
)
def test_lifetime_spec_rejects_bad_parts(part: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _life_brief(parts=[{**LIFETIME["parts"][0], **part}])


def test_cli_lifetime_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    data = json.loads(DATA.read_text(encoding="utf-8"))
    brief_path = tmp_path / "led_loop.brief.json"
    brief_path.write_text(json.dumps({**data, "lifetime": LIFETIME}), encoding="utf-8")
    assert (
        cli.main(
            [
                "sim-request",
                "--brief",
                str(brief_path),
                "--kind",
                "lifetime",
                "--out-dir",
                str(tmp_path / "sim"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    _life_answer(tmp_path, DesignBrief.model_validate({**data, "lifetime": LIFETIME}), "pass")
    assert cli.main(["sim-check", "--brief", str(brief_path), "--kind", "lifetime"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
