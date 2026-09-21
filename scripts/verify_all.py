"""Run the canonical repository verification stages."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Sequence

STAGES: dict[str, tuple[tuple[str, ...], ...]] = {
    "docs": (
        ("uv", "run", "python", "scripts/verify_docs.py"),
        ("uv", "run", "python", "scripts/render_konnect_tools.py", "--check"),
        ("git", "diff", "--check"),
    ),
    "fast": (
        ("uv", "run", "ruff", "check"),
        ("uv", "run", "ruff", "format", "--check"),
        ("uv", "run", "pyright"),
        ("uv", "run", "pytest"),
        ("uv", "run", "python", "scripts/verify_docs.py"),
        ("git", "diff", "--check"),
    ),
    "standard": (
        ("uv", "run", "ruff", "check"),
        ("uv", "run", "ruff", "format", "--check"),
        ("uv", "run", "pyright"),
        ("uv", "run", "pytest"),
        ("uv", "run", "pytest", "tests/integration", "-m", "docker", "-n", "0"),
        ("uv", "run", "python", "scripts/verify_docs.py"),
        ("git", "diff", "--check"),
    ),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=tuple(STAGES), default="fast")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        print(
            json.dumps(
                {
                    stage: [list(command) for command in commands]
                    for stage, commands in STAGES.items()
                },
                indent=2,
            )
        )
        return 0
    if args.stage == "standard" and not os.environ.get("CIRCUIT_TOOLS_IMAGE"):
        print("CIRCUIT_TOOLS_IMAGE is required for the standard stage")
        return 2
    for command in STAGES[args.stage]:
        print("$ " + " ".join(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
