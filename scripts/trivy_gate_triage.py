#!/usr/bin/env python3
"""Classify Trivy gate findings for the publish <image> Trivy step.

Reads a Trivy JSON report (the `*-trivy` run artifact or a local
`trivy image --format json` output) and groups the findings the publish
gate would fail on — HIGH/CRITICAL vulnerabilities with a non-empty
FixedVersion, i.e. `severity: CRITICAL,HIGH` + `ignore-unfixed: true` —
into the disposition classes this family uses:

  re-resolve   Python packages installed by `uv sync --frozen` from a
               lock (agent-server .venv): bump in the lock build step.
  dockerfile   OS packages and other image layers: fix with a Dockerfile
               layer (apt upgrade, vendored-tool patch).
  waiver       Upstream vendored payload no released artifact fixes
               (node_modules inside nodejs_wheel/acp-node/openvscode,
               ensurepip bundles, shaded jars, Go binaries): needs a
               `.trivyignore` `exp:` entry plus a docs/operations.md note.
  review       Everything else: decide case by case.

Usage: scripts/trivy_gate_triage.py report.json [--exp YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SEVERITIES = {"CRITICAL", "HIGH"}

_RE_RESOLVE = re.compile(r"\.venv/(lib|Lib)/|uv\.lock|agent-server/.*/site-packages", re.I)
_VENDORED_NPM = re.compile(
    r"nodejs_wheel|acp-node|openvscode|emsdk|/opt/.*/node_modules", re.I
)
_ENSUREPIP = re.compile(r"ensurepip", re.I)
_SHADED = re.compile(r"\.jar$|\.t?gz$|tsgo|compiler\.jar", re.I)


def classify(result_target: str, vuln: dict) -> tuple[str, str]:
    path = " ".join(
        str(vuln.get(k, "")) for k in ("PkgPath", "PkgName", "Target")
    ) + " " + result_target
    if _RE_RESOLVE.search(path):
        return "re-resolve", "lock-resolved python package (agent-server .venv)"
    if _ENSUREPIP.search(path):
        return "waiver", "ensurepip bundled wheel (upstream release content)"
    if _VENDORED_NPM.search(path):
        return "waiver", "vendored node_modules tree (no upstream fix yet)"
    if _SHADED.search(path):
        return "waiver", "shaded/bundled payload inside a vendored binary"
    if vuln.get("PkgType", "").startswith(("debian", "apk", "ubuntu")):
        return "dockerfile", "OS package — Dockerfile apt upgrade layer covers"
    return "review", "no matching disposition class"


def iter_findings(report: dict):
    for result in report.get("Results", []):
        target = str(result.get("Target", ""))
        cls = result.get("Class", "")
        for v in result.get("Vulnerabilities") or []:
            yield target, cls, v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", help="Trivy JSON report path (- for stdin)")
    ap.add_argument("--exp", default=None, help="Expiry date for suggested waivers")
    ap.add_argument(
        "--all", action="store_true",
        help="also list unfixed findings (normally ignored by the gate)",
    )
    args = ap.parse_args()

    text = sys.stdin.read() if args.report == "-" else Path(args.report).read_text(
        encoding="utf-8"
    )
    report = json.loads(text)

    rows = []
    for target, cls, v in iter_findings(report):
        if v.get("Severity") not in SEVERITIES:
            continue
        fixed = v.get("FixedVersion") or ""
        if not args.all and not fixed:
            continue
        disposition, why = classify(target, v)
        if not fixed:
            disposition = "unfixed"
            why = "no fixed version — gate ignores; keep listed for awareness"
        rows.append(
            {
                "id": v.get("VulnerabilityID", "?"),
                "pkg": v.get("PkgName", "?"),
                "installed": v.get("InstalledVersion", "?"),
                "fixed": fixed or "-",
                "sev": v.get("Severity", "?"),
                "where": v.get("PkgPath") or target,
                "class": cls,
                "disposition": disposition,
                "why": why,
            }
        )

    rows.sort(key=lambda r: (r["disposition"], r["id"]))
    by = {}
    for r in rows:
        by.setdefault(r["disposition"], []).append(r)

    print(f"# Trivy gate triage — {len(rows)} gate-relevant findings")
    print()
    for disp in ("re-resolve", "dockerfile", "waiver", "review", "unfixed"):
        group = by.get(disp, [])
        if not group:
            continue
        print(f"## {disp} ({len(group)})")
        print()
        print("| id | package | installed | fixed | where |")
        print("| --- | --- | --- | --- | --- |")
        for r in group:
            print(
                f"| {r['id']} | {r['pkg']} | {r['installed']} | "
                f"{r['fixed']} | {r['where']} |"
            )
        print()

    waiver_ids = sorted({r["id"] for r in by.get("waiver", [])})
    if waiver_ids:
        exp = args.exp or "YYYY-MM-DD"
        print("## Suggested .trivyignore block")
        print()
        print("```")
        for vid in waiver_ids:
            print(f"{vid} exp:{exp}")
        print("```")
        print()
        print(
            "Record the rationale in docs/operations.md next to the existing "
            "hardening entries, and re-verify on the next upstream bump."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
