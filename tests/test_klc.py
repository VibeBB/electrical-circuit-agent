from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

import circuit.klc as klc
import circuit.libverify as libverify
from circuit.klc import KlcReport, KlcViolation, run_klc


def _checker_tree(root: Path, kind: str = "footprint") -> Path:
    script = root / "klc-check" / f"check_{kind}.py"
    script.parent.mkdir(parents=True)
    script.write_text("pass\n", encoding="utf-8")
    return script


def test_run_klc_parses_junit_and_maps_rule_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "klc"
    _checker_tree(root)
    monkeypatch.setattr(klc, "KLC_ROOT", root)

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        junit_path = Path(args[args.index("--junit") + 1])
        junit_path.write_text(
            "<testsuites><testsuite>"
            '<testcase name="F5.1.1 silk"><failure message="silk issue"/></testcase>'
            '<testcase name="F6.2 smd"><failure message="smd issue"/></testcase>'
            '<testcase name="F7.3 through-hole"><failure message="tht issue"/></testcase>'
            '<testcase name="S4.1 symbol"><failure message="pin issue"/></testcase>'
            '<testcase name="F1.2 naming"><failure message="name issue"/></testcase>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="colored output", stderr="")

    monkeypatch.setattr(klc.subprocess, "run", fake_run)

    report = run_klc("footprint", tmp_path / "part.kicad_mod")

    assert report.commit == klc.KLC_COMMIT
    assert report.exit_code == 1
    assert [(item.rule, item.severity) for item in report.violations] == [
        ("F5.1.1", "error"),
        ("F6.2", "error"),
        ("F7.3", "error"),
        ("S4.1", "error"),
        ("F1.2", "warning"),
    ]


def test_run_klc_reports_missing_checker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(klc, "KLC_ROOT", tmp_path / "missing")

    report = run_klc("symbol", tmp_path / "part.kicad_sym")

    assert report.violations[0].rule == "klc_unavailable"
    assert report.violations[0].severity == "error"


def test_run_klc_fails_closed_when_nonzero_exit_has_no_junit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "klc"
    _checker_tree(root, "symbol")
    monkeypatch.setattr(klc, "KLC_ROOT", root)

    def failed_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=2, stdout="red error", stderr="")

    monkeypatch.setattr(klc.subprocess, "run", failed_run)

    report = run_klc("symbol", tmp_path / "part.kicad_sym")

    assert report.violations[0].rule == "klc_failed"


def test_run_klc_fails_closed_on_junit_parser_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "klc"
    _checker_tree(root)
    monkeypatch.setattr(klc, "KLC_ROOT", root)

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        Path(args[args.index("--junit") + 1]).write_text(
            "<testsuite><testcase name='parse'><error>Traceback (most recent call last)</error>"
            "</testcase></testsuite>",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(klc.subprocess, "run", fake_run)

    report = run_klc("footprint", tmp_path / "nightly.kicad_mod")

    assert report.violations[0].rule == "klc_failed"


def test_run_klc_ignores_colored_stdout_when_junit_is_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "klc"
    _checker_tree(root)
    monkeypatch.setattr(klc, "KLC_ROOT", root)

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        Path(args[args.index("--junit") + 1]).write_text(
            "<testsuite tests='0' failures='0'/>",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="\x1b[31mF5.1 failed\x1b[0m", stderr="")

    monkeypatch.setattr(klc.subprocess, "run", fake_run)

    report = run_klc("footprint", tmp_path / "part.kicad_mod")

    assert report.violations == []


def test_libverify_maps_klc_rules_to_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_klc(_kind: Literal["footprint", "symbol"], _path: Path) -> KlcReport:
        return KlcReport(
            commit=klc.KLC_COMMIT,
            exit_code=1,
            violations=[
                KlcViolation(rule="F5.1.1", severity="error", message="silk issue"),
                KlcViolation(rule="F1.2", severity="warning", message="name issue"),
                KlcViolation(rule="klc_unavailable", severity="error", message="missing"),
            ],
        )

    monkeypatch.setattr(
        libverify,
        "run_klc",
        fake_run_klc,
    )
    findings: list[libverify.VerifyFinding] = []

    libverify._check_klc(  # pyright: ignore[reportPrivateUsage]
        "footprint", Path("part.kicad_mod"), findings
    )

    assert [(item.code, item.severity) for item in findings] == [
        ("klc_F5.1.1", "error"),
        ("klc_F1.2", "warning"),
        ("klc_unavailable", "error"),
    ]
