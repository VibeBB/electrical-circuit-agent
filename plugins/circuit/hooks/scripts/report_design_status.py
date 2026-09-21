#!/usr/bin/env python3
"""Report deterministic design-report verdicts when an agent stops."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SKIP_DIRECTORIES = {".git", ".venv", "node_modules"}
MAX_DEPTH = 4


def _load_report(path: Path) -> tuple[str, str]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    try:
        from circuit.report import DesignReport
    except ImportError:
        verdict = value.get("verdict") if isinstance(value, dict) else None
    else:
        verdict = DesignReport.model_validate(value).verdict
    if not isinstance(verdict, str):
        raise ValueError(f"missing verdict in {path}")
    return str(path), verdict


def _find_reports(root: Path) -> list[Path]:
    reports: list[Path] = []
    for path in root.rglob("design-report.json"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) > MAX_DEPTH:
            continue
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file():
            reports.append(path)
    return sorted(reports)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        working_dir = Path(event.get("working_dir") or os.getcwd()).resolve()
        statuses = [_load_report(path) for path in _find_reports(working_dir)]
        if statuses:
            lines = [f"{path}: verdict={verdict}" for path, verdict in statuses]
            failed = [path for path, verdict in statuses if verdict != "pass"]
            if failed:
                lines.append(
                    "Before finishing, state each failing gate explicitly for: " + ", ".join(failed)
                )
            context = "\n".join(lines)
        else:
            context = f"No design reports found under {working_dir}."
        print(json.dumps({"decision": "allow", "additionalContext": context}))
        return 0
    except Exception as exc:
        print(f"report_design_status: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
