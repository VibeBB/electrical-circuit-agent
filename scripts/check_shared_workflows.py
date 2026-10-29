from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED: dict[str, str] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        "6094125a1d9cb242205566fb4f87f011f5acb0df4a9321d174b6df145ddd8cd2"
    ),
    ".github/workflows/workflow-lint.yml": (
        "d767280f031ad3ff7e16949b9112ca7e12513f62ba2c89dc6375143c16c75520"
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        "9f9a5fa4339e68b7e0b12ffb90733b7cdc51d3da1dec10197c730bd0dd655385"
    ),
    ".github/workflows/dependency-review.yml": (
        "f6096273b285116ebd441c9ffb76bd9b8aed0dec555fe1e2f3a98b6080448638"
    ),
    ".github/workflows/scorecard.yml": (
        "55df339e5b8f8918b3bdaa52f6fae2bf99a02a79921aae9e3c05d6e2e6bc3ffc"
    ),
    ".github/workflows/codeql.yml": (
        "083e0dfcd6f5f6bbddc8b949d8a4e73736f14073cc1be02559ce8077db92274e"
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
