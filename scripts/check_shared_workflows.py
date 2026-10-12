from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED: dict[str, str] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        "05f760fe728dd14dc734a0764fe98c6eb129ef70080dedd0f38ed4efb810ef40"
    ),
    ".github/workflows/workflow-lint.yml": (
        "c351b07455ccbb481de52b2e6f40492bce4cc84be4681f44ab304a9ba722e05f"
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        "35a55de9132d869d7330de912d90a7e89982b4fbce88eaf5ff11fd83d1eb6f77"
    ),
    ".github/workflows/dependency-review.yml": (
        "8574255851308ddc4fe61892b74c681e5dada10e1c0b7fe83c5cff5e7b59deb8"
    ),
    ".github/workflows/scorecard.yml": (
        "067b786c95c213b2d2106de16059cd0293a3fa8f80b900f9bcbf862351017a88"
    ),
    ".github/workflows/codeql.yml": (
        "79776d978b19eb0d33cfaeb3c8a3adfa9996a6b71db32b2c45d95675897150fb"
    ),
}
UNITS: dict[str, tuple[str, bytes | None]] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        ".github/workflows/pr-branch-cleanup.yml",
        None,
    ),
    ".github/workflows/workflow-lint.yml": (
        ".github/workflows/workflow-lint.yml",
        None,
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        ".github/workflows/main-ci-failure-issue.yml",
        b"jobs:",
    ),
    ".github/workflows/dependency-review.yml": (
        ".github/workflows/dependency-review.yml",
        None,
    ),
    ".github/workflows/scorecard.yml": (
        ".github/workflows/scorecard.yml",
        None,
    ),
    ".github/workflows/codeql.yml": (
        ".github/workflows/codeql.yml",
        None,
    ),
}


def _read_unit(root: Path, path: str, marker: bytes | None) -> bytes:
    content = (root / path).read_bytes()
    if marker is None:
        return content
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == marker:
            return content[offset:]
        offset += len(line)
    raise ValueError("jobs: marker not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print",
        action="store_true",
        help="print current SHA-256 hashes without checking EXPECTED",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for unit, (path, marker) in UNITS.items():
        try:
            content = _read_unit(root, path, marker)
        except (OSError, ValueError) as exc:
            actual = "missing"
            if args.print:
                print(f"{unit}: {actual}")
            else:
                failure = f"{path}: expected {EXPECTED[unit]}, got {actual} ({exc})"
                failures.append(failure)
            continue
        actual = hashlib.sha256(content).hexdigest()
        if args.print:
            print(f"{unit}: {actual}")
        elif actual != EXPECTED[unit]:
            failures.append(f"{path}: expected {EXPECTED[unit]}, got {actual}")
    if args.print:
        return 1 if failures else 0
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("shared workflows match canonical hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
