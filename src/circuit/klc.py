"""Subprocess integration for the pinned KiCad Library Convention checker."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

KLC_COMMIT = "90b0af91eaffcd91552027c3bfd166896f78c7de"
KLC_ROOT = Path("/opt/kicad-library-utils")
_SCRIPTS = {
    "footprint": Path("klc-check/check_footprint.py"),
    "symbol": Path("klc-check/check_symbol.py"),
}
_RULE_ID = re.compile(r"\b([FS]\d+(?:\.\d+)*)\b", re.IGNORECASE)
_FUNCTIONAL_GROUPS = ("F5", "F6", "F7", "S4")


class KlcViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    severity: Literal["error", "warning"]
    message: str


class KlcReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str
    exit_code: int
    violations: list[KlcViolation]


def _failure_report(rule: Literal["klc_unavailable", "klc_failed"], message: str) -> KlcReport:
    return KlcReport(
        commit=KLC_COMMIT,
        exit_code=127 if rule == "klc_unavailable" else 1,
        violations=[KlcViolation(rule=rule, severity="error", message=message)],
    )


def _parse_junit(path: Path) -> list[KlcViolation]:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError(f"unsupported JUnit root element: {root.tag}")
    violations: list[KlcViolation] = []
    for testcase in root.iter("testcase"):
        name = testcase.attrib.get("name", "")
        for result in testcase:
            if result.tag not in {"failure", "error"}:
                continue
            text = " ".join(
                part
                for part in (
                    name,
                    result.attrib.get("message", ""),
                    "".join(result.itertext()),
                )
                if part
            ).strip()
            if result.tag == "error" or "traceback (most recent call last)" in text.casefold():
                raise ValueError(f"KLC checker raised an exception in {name or 'a test case'}")
            match = _RULE_ID.search(text)
            rule = match.group(1).upper() if match is not None else name or "unknown"
            severity: Literal["error", "warning"] = (
                "error" if rule.upper().startswith(_FUNCTIONAL_GROUPS) else "warning"
            )
            message = (
                result.attrib.get("message", "").strip()
                or "".join(result.itertext()).strip()
                or name
                or "KLC reported a violation without a message"
            )
            violations.append(KlcViolation(rule=rule, severity=severity, message=message))
    return violations


def run_klc(kind: Literal["footprint", "symbol"], path: Path) -> KlcReport:
    """Run the pinned upstream checker and parse only its JUnit XML report."""

    script = KLC_ROOT / _SCRIPTS[kind]
    if not script.is_file():
        return _failure_report("klc_unavailable", f"KLC checker is missing: {script}")

    with tempfile.TemporaryDirectory(prefix="circuit-klc-") as temporary:
        junit_path = Path(temporary) / "klc-results.xml"
        command = [
            sys.executable,
            str(script),
            str(path),
            "--junit",
            str(junit_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return _failure_report("klc_failed", f"KLC checker could not complete: {error}")

        if not junit_path.is_file():
            return _failure_report(
                "klc_failed",
                f"KLC checker exited {result.returncode} without a JUnit report",
            )
        try:
            violations = _parse_junit(junit_path)
        except (ET.ParseError, OSError, ValueError) as error:
            return _failure_report("klc_failed", f"KLC JUnit report is unusable: {error}")
        if result.returncode != 0 and not violations:
            return _failure_report(
                "klc_failed",
                f"KLC checker exited {result.returncode} without reported violations",
            )
        return KlcReport(
            commit=KLC_COMMIT,
            exit_code=result.returncode,
            violations=violations,
        )
