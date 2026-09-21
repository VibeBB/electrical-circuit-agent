#!/usr/bin/env python3
"""Render the machine-readable Konnect coverage matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "plugins/circuit/skills/circuit-konnect/references/konnect-tools.json"
DOC = ROOT / "docs/konnect-tools.md"
ROLES = [
    "authoring",
    "authoring_conditional",
    "advisory",
    "export_comparison",
    "library",
    "lifecycle",
    "excluded",
]


def render(matrix: list[dict[str, str]]) -> str:
    lines = [
        "# Konnect v0.12.1 tool coverage",
        "",
        "This table classifies every Konnect v0.12.1 tool by role, using",
        "`plugins/circuit/skills/circuit-konnect/references/konnect-tools.json` as the",
        "source of truth. Konnect output is advisory and never changes `kicad-cli` JSON verdicts.",
        "",
    ]
    by_role = {role: [item for item in matrix if item["role"] == role] for role in ROLES}
    for role in ROLES:
        entries = by_role[role]
        lines.append(f"## {role}")
        lines.append("")
        lines.append("| Tool | Category | Stage | IPC | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        if entries:
            for item in entries:
                note = item["note"].replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| `{item['tool']}` | {item['category']} | {item['stage']} | "
                    f"{item['ipc']} | {note} |"
                )
        else:
            lines.append("| _none_ | — | — | — | — |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    output = render(matrix)
    if args.write:
        DOC.write_text(output, encoding="utf-8")
    if args.check:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != output:
            print(f"{DOC} is out of date")
            return 1
    elif not args.write:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
