from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit import cli

DATA = Path(__file__).parent / "data"


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_cli_doctor_warn_never_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["doctor", "--warn"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] in ("ok", "fail")
    assert payload["checks"]


def test_cli_doctor_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert payload["status"] in ("ok", "fail")
    assert isinstance(payload["checks"], list)


def test_cli_intake_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(
        [
            "intake",
            "--brief",
            str(DATA / "brief_led_loop.json"),
            "--intake",
            str(DATA / "brief_led_loop.intake.json"),
        ],
        capsys,
    )
    assert code == 0
    assert payload["verdict"] == "pass"


def test_cli_intake_missing_file_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        [
            "intake",
            "--brief",
            str(DATA / "brief_led_loop.json"),
            "--intake",
            str(tmp_path / "absent.json"),
        ],
        capsys,
    )
    assert code == 1
    assert payload["verdict"] == "fail"
    assert payload["stage"] == "intake"


def test_cli_intake_blocked_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad_intake = tmp_path / "bad.intake.json"
    bad_intake.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "brief_sha256": "0" * 64,
                "part_sources": {},
                "net_sources": {},
                "requirements": [],
                "assumptions": [],
                "open_questions": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    code, payload = _run(
        [
            "intake",
            "--brief",
            str(DATA / "brief_led_loop.json"),
            "--intake",
            str(bad_intake),
        ],
        capsys,
    )
    assert code == 1
    assert payload["verdict"] == "fail"


def test_cli_sch_lint_missing_file_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        ["sch-lint", str(tmp_path / "absent.kicad_sch")],
        capsys,
    )
    assert code == 1
    assert payload["verdict"] == "fail"


def test_cli_connectivity_writes_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "sensor.connectivity-source.json"
    code, payload = _run(
        [
            "connectivity",
            "--brief",
            str(DATA / "brief_led_loop.json"),
            "--out",
            str(out),
        ],
        capsys,
    )
    assert code == 0
    assert payload["verdict"] == "pass"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["system"] == "circuit"
    assert written["connectors"]
    assert written["nets"]


def test_cli_author_subprocess_failure_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    code, payload = _run(
        [
            "author",
            "--brief",
            str(DATA / "brief_led_loop.json"),
            "--workdir",
            str(tmp_path / "work"),
        ],
        capsys,
    )
    assert code == 1
    assert payload["verdict"] == "fail"
    assert payload["stage"] == "author"
