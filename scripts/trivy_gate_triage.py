#!/usr/bin/env python3
"""Triage Trivy image-gate findings for the publish workflows.

Reads a Trivy JSON report (from ``trivy image --format json``) and groups
HIGH/CRITICAL findings into the handling buckets the family uses:

- ``re-resolve``: packages inside the OpenHands SDK virtualenv or lock —
  handle by re-resolving the SDK uv.lock floor pins in the publish step.
- ``dockerfile``: OS/language packages fixable by rebuilding the image
  with updated pins (apt, uv, npm trees patched in the Dockerfile).
- ``vendored``: content shipped inside an upstream release tarball
  (nodejs_wheel, acp-node, openvscode, emsdk node trees) — cannot be
  fixed locally; needs a `.trivyignore` waiver with an `exp:` date and an
  ops note.
- ``ensurepip``/``shaded``: bundled wheels or shaded JAR classes — also
  waiver-only, but called out separately for review.

Usage:

    python3 scripts/trivy_gate_triage.py trivy-image.json

Prints a markdown table per bucket plus a suggested `.trivyignore` block.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

_RE_RESOLVE = re.compile(r"\.venv/(lib|Lib)/|uv\.lock|agent-server/.*/site-packages", re.I)
_VENDORED_NPM = re.compile(r"nodejs_wheel|acp-node|openvscode|emsdk|/opt/.*/node_modules", re.I)
_ENSUREPIP = re.compile(r"ensurepip|pkg_resources|_distutils_hack|bundled.*whl", re.I)
_SHADED = re.compile(r"\.jar!|shaded|compiler\.jar|closure", re.I)

Vuln = dict[str, Any]
Bucket = str


def _str_field(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key, "")
    return value if isinstance(value, str) else str(value)


def classify(result_target: str, vuln: Vuln) -> tuple[Bucket, str]:
    """Return (bucket, reason) for one finding."""
    haystack = (
        " ".join(
            _str_field(vuln, k) for k in ("PkgPath", "PkgName", "Target", "Class", "PkgIdentifier")
        )
        + " "
        + result_target
    )
    if _ENSUREPIP.search(haystack):
        return "ensurepip", "bundled pip/setuptools wheel inside a Python install"
    if _SHADED.search(haystack):
        return "shaded", "class inside a shaded JAR or bundled archive"
    if _RE_RESOLVE.search(haystack):
        return "re-resolve", "path inside the SDK .venv/uv.lock — fix by lock re-resolution"
    if _VENDORED_NPM.search(haystack):
        return "vendored", "inside an upstream-vendored node tree — waiver candidate"
    if _str_field(vuln, "Class") in ("os-pkgs", "lang-pkgs") or _str_field(vuln, "PkgName"):
        return "dockerfile", "package fixable via image rebuild with an updated pin"
    return "review", "unclassified — manual review"


def iter_findings(report: dict[str, Any]) -> Iterator[tuple[str, Vuln]]:
    """Yield (target, vuln) for every HIGH/CRITICAL vulnerability."""
    results = report.get("Results")
    if not isinstance(results, list):
        return
    for entry in cast("list[dict[str, Any]]", results):
        vulns = entry.get("Vulnerabilities")
        if not isinstance(vulns, list):
            continue
        target = _str_field(entry, "Target")
        for vuln in cast("list[Vuln]", vulns):
            if _str_field(vuln, "Severity").upper() in ("HIGH", "CRITICAL"):
                yield target, vuln


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|") if value is not None else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    report = cast("dict[str, Any]", json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    buckets: dict[Bucket, list[tuple[str, Vuln, str]]] = {}
    for target, vuln in iter_findings(report):
        bucket, reason = classify(target, vuln)
        buckets.setdefault(bucket, []).append((target, vuln, reason))

    for bucket in ("re-resolve", "dockerfile", "vendored", "ensurepip", "shaded", "review"):
        rows = buckets.get(bucket)
        if not rows:
            continue
        print(f"\n## {bucket} ({len(rows)})\n")
        print("| package | installed | fixed | severity | target | reason |")
        print("| --- | --- | --- | --- | --- | --- |")
        for target, vuln, reason in rows:
            print(
                "| {pkg} | {inst} | {fix} | {sev} | {tgt} | {rs} |".format(
                    pkg=_cell(vuln.get("PkgName")),
                    inst=_cell(vuln.get("InstalledVersion")),
                    fix=_cell(vuln.get("FixedVersion") or "-"),
                    sev=_cell(vuln.get("Severity")),
                    tgt=_cell(target),
                    rs=_cell(reason),
                )
            )

    waiver_rows = (
        buckets.get("vendored", []) + buckets.get("ensurepip", []) + buckets.get("shaded", [])
    )
    ids = sorted(
        {
            _str_field(vuln, "VulnerabilityID")
            for _, vuln, _ in waiver_rows
            if _str_field(vuln, "VulnerabilityID")
        }
    )
    if ids:
        print("\n## suggested .trivyignore block\n")
        print("```")
        for vid in ids:
            print(f"{vid}  # exp:YYYY-MM-DD — upstream-vendored; see docs/operations.md")
        print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
