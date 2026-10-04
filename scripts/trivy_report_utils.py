#!/usr/bin/env python3
"""Shared Trivy report helpers for the publish and container-audit workflows.

Commands:

    blocking-cves REPORT TITLE
        Render the fixable HIGH/CRITICAL findings table that the publish job
        appends to GITHUB_STEP_SUMMARY when the SARIF gate fails.
    cis-nonempty REPORT
        Exit 0 only when a --compliance JSON report contains at least one
        MisconfSummary payload. Trivy exits 0 on the empty-results anomaly,
        so the container-audit retry keys on content, not the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast


def iter_results(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield the node and every nested Results entry (compliance reports nest)."""
    yield node
    for child in cast(list[Any], node.get("Results") or []):
        if isinstance(child, dict):
            yield from iter_results(cast(dict[str, Any], child))


def blocking_cve_rows(report: dict[str, Any]) -> list[tuple[str, ...]]:
    """(CVE, package, severity, installed, fixed, target) rows that gate a publish."""
    rows: list[tuple[str, ...]] = []
    for result_any in cast(list[Any], report.get("Results") or []):
        if not isinstance(result_any, dict):
            continue
        result = cast(dict[str, Any], result_any)
        target = str(result.get("Target", "?"))
        for vuln_any in cast(list[Any], result.get("Vulnerabilities") or []):
            if not isinstance(vuln_any, dict):
                continue
            vuln = cast(dict[str, Any], vuln_any)
            if vuln.get("Severity") in ("HIGH", "CRITICAL") and vuln.get("FixedVersion"):
                rows.append(
                    (
                        str(vuln["VulnerabilityID"]),
                        str(vuln.get("PkgName", "?")),
                        str(vuln["Severity"]),
                        str(vuln.get("InstalledVersion", "?")),
                        str(vuln["FixedVersion"]),
                        target,
                    )
                )
    return rows


def render_blocking_table(title: str, rows: list[tuple[str, ...]]) -> str:
    lines = [
        f"## Blocking {title} findings",
        "",
        "| CVE | Package | Severity | Installed | Fixed | Target |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in sorted(rows))
    lines.append(f"\n{len(rows)} fixable HIGH/CRITICAL finding(s)")
    return "\n".join(lines) + "\n"


def cis_summary_totals(report: dict[str, Any]) -> dict[str, int]:
    """Sum MisconfSummary Successes/Failures across nested Results nodes."""
    passed = failed = 0
    for result in iter_results(report):
        summary = result.get("MisconfSummary")
        if not isinstance(summary, dict):
            continue
        totals = cast(dict[str, Any], summary)
        passed += int(totals.get("Successes", 0))
        failed += int(totals.get("Failures", 0))
    return {"passed": passed, "failed": failed}


def _load_report(path: Path) -> dict[str, Any]:
    report: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"trivy report must be a JSON object: {path}")
    return cast(dict[str, Any], report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    blocking = subparsers.add_parser("blocking-cves")
    blocking.add_argument("report", type=Path)
    blocking.add_argument("title")
    cis = subparsers.add_parser("cis-nonempty")
    cis.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = _load_report(args.report)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.command == "blocking-cves":
        print(render_blocking_table(args.title, blocking_cve_rows(report)), end="")
        return 0
    totals = cis_summary_totals(report)
    if totals["passed"] + totals["failed"] == 0:
        print(f"{args.report}: no MisconfSummary results", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
