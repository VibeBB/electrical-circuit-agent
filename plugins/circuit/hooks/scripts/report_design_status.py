#!/usr/bin/env python3
"""Report deterministic design-report verdicts when an agent stops."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SKIP_DIRECTORIES = {".git", ".venv", "node_modules"}
MAX_DEPTH = 4
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}


def _load_report(path: Path) -> tuple[str, str, list[str]]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    try:
        from circuit.report import DesignReport
    except ImportError:
        verdict = value.get("verdict") if isinstance(value, dict) else None
        missing: list[str] = []
    else:
        design = DesignReport.model_validate(value)
        verdict = design.verdict
        missing = _missing_sections(design)
    if not isinstance(verdict, str):
        raise ValueError(f"missing verdict in {path}")
    return str(path), verdict, missing


def _missing_sections(design: Any) -> list[str]:
    missing = []
    if design.sch_lint is None:
        missing.append("sch_lint")
    if not design.exports:
        missing.append("exports")
    if not design.renders:
        missing.append("renders")
    if design.jobset is None:
        missing.append("jobset")
    if not design.advisory:
        missing.append("advisory")
    return missing


def _review_record_for(image: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", image.stem.lower()).strip("-") or "image"
    return image.parent / f"review-visual-{slug}.advisory.json"


def _unreviewed_images(report_dir: Path) -> list[Path]:
    """Rendered images near the report lacking a review-visual record."""
    directories = [report_dir]
    reports_dir = report_dir / "circuit-reports"
    if reports_dir.is_dir():
        directories.append(reports_dir)
    return sorted(
        image
        for directory in directories
        for image in directory.iterdir()
        if image.is_file()
        and image.suffix.lower() in IMAGE_SUFFIXES
        and not _review_record_for(image).exists()
    )


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
            lines = [f"{path}: verdict={verdict}" for path, verdict, _missing in statuses]
            incomplete = [(path, missing) for path, _verdict, missing in statuses if missing]
            if incomplete:
                lines.append(
                    "Design report sections not recorded (run the remaining"
                    " pipeline stages or record the files): "
                    + "; ".join(f"{path}: {', '.join(missing)}" for path, missing in incomplete)
                )
            failed = [path for path, verdict, _missing in statuses if verdict != "pass"]
            if failed:
                lines.append(
                    "Before finishing, state each failing gate explicitly for: " + ", ".join(failed)
                )
            unreviewed = [
                image
                for path, _verdict, _missing in statuses
                for image in _unreviewed_images(Path(path).parent)
            ]
            if unreviewed:
                lines.append(
                    "Rendered images without a review-visual-*.advisory.json "
                    "record: "
                    + "; ".join(str(image) for image in unreviewed)
                    + " — vision-review every rendered image and write the "
                    "record via the review-record CLI before finishing."
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
