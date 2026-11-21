from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED: dict[str, str] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        "7211ba41aa5b1c59b4102802e3d3bdfc4c97731d3347280c64e77905b73ade46"
    ),
    ".github/workflows/workflow-lint.yml": (
        "3900021262a5d1983f621a2fa68c85a40b8d2a3a1720c6f9bb3b2071045dc5cf"
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        "3ff46930fb522e96a7a4241f75d4bc4d706e5b7d9ce463d07a76dab8bd959a2a"
    ),
    ".github/workflows/dependency-review.yml": (
        "81448df17866313f39b0cc55fcf764d8e347720f3232b9eb55a2b6ebc56ca08b"
    ),
    ".github/workflows/scorecard.yml": (
        "946a91aa72e0cd48f8347a911ee83531c4749b288535c65be7ca8d7d18674502"
    ),
    ".github/workflows/codeql.yml": (
        "6f6de3c4c0633b129b6e9ac948efc2a1bbaa06ec2326e29883bf2f7c77c15a13"
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
